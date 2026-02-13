"""
Main daemon loop for vm-manager.

Orchestrates all components and manages the daemon lifecycle:
- Event-driven VM monitoring (start/stop/reboot events)
- Startup scan of existing running VMs (--check-existing)
- Periodic stale tag scanning (--stale-scan-interval)
- Boot management modes (--boot-at-start, --boot-always)
"""

import asyncio
import logging
import signal
import sys
from typing import List, Optional
import libvirt

from ansible_deployer.metadata_manager import MetadataManager
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
        on_broken: Optional[str] = None,
        stale_scan_interval: int = 300
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
            stale_scan_interval: Seconds between stale tag scans (0 = disabled)
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
        self.stale_scan_interval = stale_scan_interval
        
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
        self._background_tasks: List[asyncio.Task] = []
    
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
                task = asyncio.create_task(self._continuous_boot_loop())
                self._background_tasks.append(task)
            
            # Start stale tag scan loop
            if self.stale_scan_interval > 0:
                task = asyncio.create_task(self._stale_tag_scan_loop())
                self._background_tasks.append(task)
        
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
        
        # Cancel background tasks (boot loop, stale scan loop)
        for task in self._background_tasks:
            task.cancel()
        for task in self._background_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._background_tasks.clear()
        
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
    
    def _is_vm_stale(self, domain: libvirt.virDomain) -> bool:
        """
        Check if a VM has a stale 'used' tag that should be cleaned up.
        
        A VM is stale if it has a removable tag (e.g., 'used') in its
        inactive XML but is NOT actively being used by ansible-deployer
        (in_use metadata is false or absent).
        
        Args:
            domain: The domain to check
            
        Returns:
            True if the VM has a stale tag that should be removed
        """
        try:
            # Check if any removable tags exist in the inactive XML
            vm_tags = get_vm_tags(domain)
            if not any(tag in vm_tags for tag in self.tags_to_remove):
                return False
            
            # Check metadata — if in_use is true, it's actively being used
            try:
                metadata_mgr = MetadataManager(domain)
                if metadata_mgr.is_in_use():
                    return False
            except Exception:
                pass  # No metadata or error reading it — treat as stale
            
            return True
            
        except Exception as e:
            logger.warning(f"Error checking if VM {domain.name()} is stale: {e}")
            return False
    
    async def _check_existing_vms(self) -> None:
        """
        Check existing running VMs at startup (--check-existing mode).
        
        Scans all running VMs and processes any that match filters.
        VMs that are actively in use (in_use=true) are monitored via SSH.
        VMs with stale tags (in_use=false or no metadata) get tags removed directly.
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
                    if not self._should_monitor_vm(domain):
                        continue
                    
                    vm_name = domain.name()
                    
                    if self._is_vm_stale(domain):
                        # Stale tag — remove directly without SSH wait
                        logger.info(
                            f"VM {vm_name} has stale removable tags "
                            f"(in_use=false or no metadata), removing directly"
                        )
                        await self.tag_cleaner.remove_stale_tags(domain)
                    else:
                        # Check if it has removable tags and is actively in use
                        vm_tags = get_vm_tags(domain)
                        has_removable = any(tag in vm_tags for tag in self.tags_to_remove)
                        if has_removable:
                            logger.info(f"Processing existing VM (actively in use): {vm_name}")
                            await self.tag_cleaner.handle_vm_started(domain)
                except Exception as e:
                    logger.error(
                        f"Error processing existing VM: {e}",
                        exc_info=True
                    )
        
        except Exception as e:
            logger.error(f"Error checking existing VMs: {e}", exc_info=True)
    
    async def _stale_tag_scan_loop(self) -> None:
        """
        Periodically scan all VMs and remove stale 'used' tags.
        
        A tag is stale when:
        - VM has a removable tag (e.g., 'used') in its inactive XML
        - VM metadata shows in_use=false (or has no metadata)
        - VM is not currently being monitored by an active SSH-wait task
        
        This catches VMs where the deployer finished but the VM was never
        rebooted, so vm-manager never got an event to trigger cleanup.
        """
        logger.info(
            f"Started stale tag scan loop (interval: {self.stale_scan_interval}s)"
        )
        
        try:
            while self._running:
                await asyncio.sleep(self.stale_scan_interval)
                
                if not self._running:
                    break
                
                try:
                    await self._run_stale_tag_scan()
                except Exception as e:
                    logger.error(f"Error in stale tag scan: {e}", exc_info=True)
        
        except asyncio.CancelledError:
            logger.info("Stale tag scan loop cancelled")
        finally:
            logger.info("Stale tag scan loop stopped")
    
    async def _run_stale_tag_scan(self) -> None:
        """
        Run a single stale tag scan across all running VMs.
        """
        try:
            domains = self.conn.listAllDomains(
                libvirt.VIR_CONNECT_LIST_DOMAINS_RUNNING
            )
        except Exception as e:
            logger.error(f"Failed to list domains for stale scan: {e}")
            return
        
        cleaned = 0
        for domain in domains:
            try:
                if not self._should_monitor_vm(domain):
                    continue
                
                vm_name = domain.name()
                vm_uuid = domain.UUIDString()
                
                # Skip VMs currently being monitored (SSH wait in progress)
                if self.vm_tracker and await self.vm_tracker.is_monitoring(vm_uuid):
                    continue
                
                if self._is_vm_stale(domain):
                    logger.info(
                        f"Stale tag scan: VM {vm_name} has stale removable tags, "
                        "removing directly"
                    )
                    await self.tag_cleaner.remove_stale_tags(domain)
                    cleaned += 1
            
            except Exception as e:
                logger.error(f"Error scanning VM for stale tags: {e}")
        
        if cleaned > 0:
            logger.info(f"Stale tag scan: cleaned {cleaned} VM(s)")
        else:
            logger.debug("Stale tag scan: no stale tags found")
    
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
    on_broken: Optional[str] = None,
    stale_scan_interval: int = 300
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
        stale_scan_interval: Seconds between stale tag scans (0 = disabled)
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
        on_broken=on_broken,
        stale_scan_interval=stale_scan_interval
    )
    
    # Setup signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    
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
