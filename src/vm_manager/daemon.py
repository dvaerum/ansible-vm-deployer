"""
Main daemon loop for vm-manager.

Orchestrates all components and manages the daemon lifecycle.
"""

import asyncio
import logging
import signal
import sys
from typing import List, Optional
import libvirt

from vm_tools_common.libvirt_connection import LibvirtConnection
from vm_tools_common.tag_filters import vm_matches_tags
from vm_tools_common.vm_operations import get_vm_tags

from .ssh_checker import SSHChecker, SSHConfig
from .vm_tracker import VMTracker
from .event_monitor import EventMonitor
from .tag_cleaner import TagCleaner

logger = logging.getLogger(__name__)


class VMManagerDaemon:
    """
    Main daemon for VM monitoring and tag management.
    
    Coordinates all components:
    - EventMonitor: Watches for VM start/stop events
    - SSHChecker: Verifies SSH connectivity
    - VMTracker: Tracks active monitoring sessions
    - TagCleaner: Orchestrates SSH checks and tag removal
    """
    
    def __init__(
        self,
        libvirt_uri: str,
        ssh_config: SSHConfig,
        monitor_tags: List[str],
        exclude_tags: List[str],
        tags_to_remove: List[str],
        check_interval: int,
        max_wait_time: Optional[int],
        check_existing: bool,
        boot_at_start: bool,
        boot_always: bool,
        broken_tag: Optional[str] = None,
        on_broken: Optional[str] = None
    ):
        """
        Initialize the daemon.
        
        Args:
            libvirt_uri: Libvirt connection URI
            ssh_config: SSH authentication configuration
            monitor_tags: Tags to monitor (VM must have at least one)
            exclude_tags: Tags to exclude (VM must have none)
            tags_to_remove: Tags to remove after SSH succeeds
            check_interval: Seconds between SSH retry attempts
            max_wait_time: Maximum seconds to wait for SSH (None = infinite)
            check_existing: Check existing running VMs at startup
            boot_at_start: Boot matching shutdown VMs once at startup
            boot_always: Continuously boot matching shutdown VMs
            broken_tag: Tag to add when SSH times out (None = don't tag)
            on_broken: Path to external script to run when a VM is marked broken (None = disabled)
        """
        self.libvirt_uri = libvirt_uri
        self.ssh_config = ssh_config
        self.monitor_tags = monitor_tags
        self.exclude_tags = list(exclude_tags)  # Copy to avoid mutating caller's list
        self.tags_to_remove = tags_to_remove
        self.check_interval = check_interval
        self.max_wait_time = max_wait_time
        self.check_existing = check_existing
        self.boot_at_start = boot_at_start
        self.boot_always = boot_always
        self.broken_tag = broken_tag
        self.on_broken = on_broken
        
        # Auto-exclude broken VMs from monitoring/booting.
        # Without this, the daemon would re-monitor broken VMs on every
        # reboot or --check-existing cycle, wait for SSH to time out again,
        # and re-add the broken tag — creating an infinite loop.
        if self.broken_tag and self.broken_tag not in self.exclude_tags:
            self.exclude_tags.append(self.broken_tag)
            logger.info(
                f"Auto-excluding VMs with '{self.broken_tag}' tag from monitoring"
            )
        
        # Components (initialized in start())
        self.conn: Optional[libvirt.virConnect] = None
        self.ssh_checker: Optional[SSHChecker] = None
        self.vm_tracker: Optional[VMTracker] = None
        self.event_monitor: Optional[EventMonitor] = None
        self.tag_cleaner: Optional[TagCleaner] = None
        
        # Lifecycle
        self._shutdown_event = asyncio.Event()
        self._running = False
    
    async def start(self) -> None:
        """
        Start the daemon.
        
        Initializes all components and begins monitoring.
        """
        if self._running:
            logger.warning("Daemon is already running")
            return
        
        logger.info("Starting VM Manager daemon")
        
        try:
            # Initialize libvirt event loop (required before connecting)
            libvirt.virEventRegisterDefaultImpl()
            logger.debug("Registered libvirt default event implementation")
            
            # Connect to libvirt
            libvirt_conn = LibvirtConnection(self.libvirt_uri)
            libvirt_conn.connect()
            self.conn = libvirt_conn.get_connection()
            logger.info(f"Connected to libvirt: {self.libvirt_uri}")
            
            # Initialize components
            self.ssh_checker = SSHChecker(
                ssh_config=self.ssh_config,
                check_interval=self.check_interval,
                max_wait_time=self.max_wait_time
            )
            
            self.vm_tracker = VMTracker()
            
            self.tag_cleaner = TagCleaner(
                conn=self.conn,
                ssh_checker=self.ssh_checker,
                vm_tracker=self.vm_tracker,
                tags_to_remove=self.tags_to_remove,
                broken_tag=self.broken_tag,
                on_broken=self.on_broken,
                libvirt_uri=self.libvirt_uri
            )
            
            # Setup event monitor callbacks
            on_vm_stopped = None
            if self.boot_always:
                on_vm_stopped = self._handle_vm_stopped
            
            self.event_monitor = EventMonitor(
                conn=self.conn,
                on_vm_started=self._handle_vm_started,
                on_vm_stopped=on_vm_stopped
            )
            
            # Start event monitoring
            await self.event_monitor.start()
            
            self._running = True
            logger.info("VM Manager daemon started successfully")
            
            # Handle startup modes
            if self.check_existing:
                await self._check_existing_vms()
            
            if self.boot_at_start:
                await self._boot_matching_vms_once()
            
            # If boot_always, start the continuous boot loop
            if self.boot_always:
                asyncio.create_task(self._continuous_boot_loop())
        
        except Exception as e:
            logger.error(f"Failed to start daemon: {e}", exc_info=True)
            await self.stop()
            raise
    
    async def run(self) -> None:
        """
        Run the daemon until shutdown is requested.
        
        Waits for shutdown signal.
        """
        if not self._running:
            logger.error("Daemon not started, call start() first")
            return
        
        logger.info("Daemon running, waiting for shutdown signal...")
        
        # Wait for shutdown event
        await self._shutdown_event.wait()
        
        logger.info("Shutdown signal received")
    
    async def stop(self) -> None:
        """
        Stop the daemon and clean up resources.
        """
        if not self._running:
            return
        
        logger.info("Stopping VM Manager daemon")
        self._running = False
        
        # Stop event monitoring
        if self.event_monitor:
            await self.event_monitor.stop()
        
        # Cancel all monitoring tasks
        if self.vm_tracker:
            await self.vm_tracker.cancel_all()
        
        # Close libvirt connection
        if self.conn:
            try:
                self.conn.close()
                logger.info("Closed libvirt connection")
            except Exception as e:
                logger.warning(f"Error closing libvirt connection: {e}")
        
        logger.info("VM Manager daemon stopped")
    
    def shutdown(self) -> None:
        """
        Request daemon shutdown (can be called from signal handler).
        """
        logger.info("Shutdown requested")
        self._shutdown_event.set()
    
    def _handle_vm_started(self, domain: libvirt.virDomain) -> None:
        """
        Handle a VM start event (called by EventMonitor).
        
        Args:
            domain: The domain that started
        """
        try:
            vm_name = domain.name()
            
            # Check if VM matches our filters
            if not self._should_monitor_vm(domain):
                logger.debug(f"VM {vm_name} does not match filters, ignoring")
                return
            
            # Check if VM actually has the tags we need to remove.
            # Without this check, reboot events from reset_vm() cleanup
            # (which happen AFTER the 'used' tag was already removed)
            # would create orphaned monitor tasks that poll SSH forever.
            vm_tags = get_vm_tags(domain)
            if not any(tag in vm_tags for tag in self.tags_to_remove):
                logger.debug(
                    f"VM {vm_name} has no removable tags "
                    f"(has: {vm_tags}, need: {self.tags_to_remove}), "
                    "ignoring reboot event"
                )
                return
            
            logger.info(f"VM {vm_name} started and matches filters")
            
            # Pass to tag cleaner for processing
            asyncio.create_task(self.tag_cleaner.handle_vm_started(domain))
        
        except Exception as e:
            logger.error(f"Error handling VM start event: {e}", exc_info=True)
    
    def _handle_vm_stopped(self, domain: libvirt.virDomain) -> None:
        """
        Handle a VM stop event (called by EventMonitor in boot-always mode).
        
        Args:
            domain: The domain that stopped
        """
        try:
            vm_name = domain.name()
            logger.info(f"VM {vm_name} stopped (boot-always mode)")
            
            # In boot-always mode, we'll restart it in the continuous boot loop
        
        except Exception as e:
            logger.error(f"Error handling VM stop event: {e}", exc_info=True)
    
    def _should_monitor_vm(self, domain: libvirt.virDomain) -> bool:
        """
        Check if a VM matches the monitoring criteria.
        
        Args:
            domain: The domain to check
            
        Returns:
            True if the VM should be monitored, False otherwise
        """
        try:
            # Get VM tags
            vm_tags = get_vm_tags(domain)
            
            # Check if VM matches filters
            return vm_matches_tags(
                vm_tags=vm_tags,
                required_tags=self.monitor_tags,
                exclude_tags=self.exclude_tags
            )
        
        except Exception as e:
            logger.error(f"Error checking VM tags: {e}")
            return False
    
    def _is_vm_actively_in_use(self, domain: libvirt.virDomain) -> bool:
        """
        Check if a VM is actively being used (not a stale tag from old run).
        
        Args:
            domain: The domain to check
            
        Returns:
            True if VM is actively in use, False if tag is stale
        """
        try:
            from datetime import datetime, timedelta
            
            # Try to get metadata
            try:
                metadata_xml = domain.metadata(
                    libvirt.VIR_DOMAIN_METADATA_ELEMENT,
                    "http://example.com/vm_metadata",
                    0
                )
                
                # Parse metadata for in_use flag and started_at timestamp
                in_use = False
                started_at = None
                
                for line in metadata_xml.strip().split('\n'):
                    line = line.strip()
                    if line.startswith('in_use:'):
                        value = line.split(':', 1)[1].strip()
                        in_use = value.lower() == 'true'
                    elif line.startswith('started_at:'):
                        started_at_str = line.split(':', 1)[1].strip()
                        try:
                            # Parse ISO format timestamp
                            started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
                        except:
                            pass
                
                # If in_use is true, it's actively being used
                if in_use:
                    return True
                
                # If started_at is within last 10 minutes, consider it active
                if started_at:
                    age = datetime.now(started_at.tzinfo) - started_at
                    if age < timedelta(minutes=10):
                        return True
                
                # Metadata exists but VM is not actively in use
                logger.info(
                    f"VM {domain.name()} has 'used' tag but is not actively in use "
                    f"(in_use={in_use}, started_at={started_at}) - skipping"
                )
                return False
                
            except libvirt.libvirtError:
                # No metadata means tag might be stale, but we can't be sure
                # Skip it to be safe (avoids processing stale tags)
                logger.info(
                    f"VM {domain.name()} has 'used' tag but no metadata - "
                    "likely stale from old run, skipping"
                )
                return False
                
        except Exception as e:
            logger.warning(f"Error checking if VM {domain.name()} is in use: {e}")
            # On error, assume not in use to avoid processing stale tags
            return False
    
    async def _check_existing_vms(self) -> None:
        """
        Check existing running VMs at startup (--check-existing mode).
        
        Scans all running VMs and processes any that match filters.
        Only processes VMs that are actively being used (not stale tags).
        """
        logger.info("Checking existing running VMs")
        
        try:
            # Get all domains
            domains = self.conn.listAllDomains(
                libvirt.VIR_CONNECT_LIST_DOMAINS_RUNNING
            )
            
            logger.info(f"Found {len(domains)} running VMs")
            
            for domain in domains:
                try:
                    if self._should_monitor_vm(domain):
                        # Check if VM is actively in use (not stale tag)
                        if not self._is_vm_actively_in_use(domain):
                            continue
                        
                        vm_name = domain.name()
                        logger.info(f"Processing existing VM: {vm_name}")
                        await self.tag_cleaner.handle_vm_started(domain)
                except Exception as e:
                    logger.error(
                        f"Error processing existing VM: {e}",
                        exc_info=True
                    )
        
        except Exception as e:
            logger.error(f"Error checking existing VMs: {e}", exc_info=True)
    
    async def _boot_matching_vms_once(self) -> None:
        """
        Boot all matching shutdown VMs once at startup (--boot-at-start mode).
        """
        logger.info("Booting matching shutdown VMs (boot-at-start mode)")
        
        try:
            # Get all shut down domains
            domains = self.conn.listAllDomains(
                libvirt.VIR_CONNECT_LIST_DOMAINS_SHUTOFF
            )
            
            logger.info(f"Found {len(domains)} shutdown VMs")
            
            booted_count = 0
            for domain in domains:
                try:
                    if self._should_monitor_vm(domain):
                        vm_name = domain.name()
                        logger.info(f"Booting VM: {vm_name}")
                        domain.create()  # Start the VM
                        booted_count += 1
                except Exception as e:
                    logger.error(
                        f"Error booting VM: {e}",
                        exc_info=True
                    )
            
            logger.info(f"Booted {booted_count} matching VMs")
        
        except Exception as e:
            logger.error(f"Error in boot-at-start: {e}", exc_info=True)
    
    async def _continuous_boot_loop(self) -> None:
        """
        Continuously boot matching shutdown VMs (--boot-always mode).
        
        Runs in a background task and periodically checks for shutdown VMs.
        """
        logger.info("Started continuous boot loop (boot-always mode)")
        
        try:
            while self._running:
                try:
                    # Get all shut down domains
                    domains = self.conn.listAllDomains(
                        libvirt.VIR_CONNECT_LIST_DOMAINS_SHUTOFF
                    )
                    
                    for domain in domains:
                        try:
                            if self._should_monitor_vm(domain):
                                vm_name = domain.name()
                                logger.info(f"Booting VM: {vm_name} (boot-always)")
                                domain.create()  # Start the VM
                        except Exception as e:
                            logger.debug(f"Error booting VM in continuous loop: {e}")
                
                except Exception as e:
                    logger.error(f"Error in continuous boot loop: {e}")
                
                # Wait before checking again
                await asyncio.sleep(self.check_interval)
        
        except asyncio.CancelledError:
            logger.info("Continuous boot loop cancelled")
        finally:
            logger.info("Continuous boot loop stopped")


async def run_daemon(
    libvirt_uri: str,
    ssh_config: SSHConfig,
    monitor_tags: List[str],
    exclude_tags: List[str],
    tags_to_remove: List[str],
    check_interval: int,
    max_wait_time: Optional[int],
    check_existing: bool,
    boot_at_start: bool,
    boot_always: bool,
    broken_tag: Optional[str] = None,
    on_broken: Optional[str] = None
) -> None:
    """
    Run the VM Manager daemon.
    
    This is the main entry point that sets up signal handling and runs the daemon.
    
    Args:
        libvirt_uri: Libvirt connection URI
        ssh_config: SSH authentication configuration
        monitor_tags: Tags to monitor
        exclude_tags: Tags to exclude
        tags_to_remove: Tags to remove after SSH succeeds
        check_interval: Seconds between SSH retry attempts
        max_wait_time: Maximum seconds to wait for SSH
        check_existing: Check existing running VMs at startup
        boot_at_start: Boot matching shutdown VMs once at startup
        boot_always: Continuously boot matching shutdown VMs
        broken_tag: Tag to add when SSH times out (None = don't tag)
        on_broken: Path to external script to run when a VM is marked broken (None = disabled)
    """
    daemon = VMManagerDaemon(
        libvirt_uri=libvirt_uri,
        ssh_config=ssh_config,
        monitor_tags=monitor_tags,
        exclude_tags=exclude_tags,
        tags_to_remove=tags_to_remove,
        check_interval=check_interval,
        max_wait_time=max_wait_time,
        check_existing=check_existing,
        boot_at_start=boot_at_start,
        boot_always=boot_always,
        broken_tag=broken_tag,
        on_broken=on_broken
    )
    
    # Setup signal handlers for graceful shutdown
    loop = asyncio.get_event_loop()
    
    def signal_handler(sig):
        logger.info(f"Received signal {sig}, initiating shutdown")
        daemon.shutdown()
    
    # Register signal handlers
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))
    
    try:
        # Start the daemon
        await daemon.start()
        
        # Run until shutdown
        await daemon.run()
    
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    
    except Exception as e:
        logger.error(f"Daemon error: {e}", exc_info=True)
        sys.exit(1)
    
    finally:
        # Cleanup
        await daemon.stop()
