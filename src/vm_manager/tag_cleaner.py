"""
Tag cleanup orchestration logic.

Coordinates SSH checking and tag removal for VMs.
"""

import asyncio
import logging
import os
from typing import List, Optional
import libvirt

from vm_tools_common.vm_operations import get_vm_ip, get_vm_tags, remove_vm_tag, add_vm_tag
from vm_tools_common.exceptions import VMNotFoundException
from ansible_deployer.metadata_manager import MetadataManager
from .ssh_checker import SSHChecker
from .vm_tracker import VMTracker

logger = logging.getLogger(__name__)


class TagCleaner:
    """
    Orchestrates SSH checking and tag removal for VMs.
    
    When a VM starts, this class:
    1. Gets the VM's IP address
    2. Waits for SSH to become available
    3. Removes the specified tag from the VM
    """
    
    def __init__(
        self,
        conn: libvirt.virConnect,
        ssh_checker: SSHChecker,
        vm_tracker: VMTracker,
        tags_to_remove: List[str],
        broken_tag: Optional[str] = None,
        on_broken: Optional[str] = None,
        libvirt_uri: str = "qemu:///system"
    ):
        """
        Initialize the tag cleaner.
        
        Args:
            conn: Libvirt connection
            ssh_checker: SSH connectivity checker
            vm_tracker: VM session tracker
            tags_to_remove: List of tags to remove after SSH succeeds
            broken_tag: Tag to add when SSH times out (None = don't tag)
            on_broken: Path to external script to run when a VM is marked broken (None = disabled)
            libvirt_uri: Libvirt connection URI (passed to on_broken script as env var)
        """
        self.conn = conn
        self.ssh_checker = ssh_checker
        self.vm_tracker = vm_tracker
        self.tags_to_remove = tags_to_remove
        self.broken_tag = broken_tag
        self.on_broken = on_broken
        self.libvirt_uri = libvirt_uri
    
    async def handle_vm_started(self, domain: libvirt.virDomain) -> None:
        """
        Handle a VM start event.
        
        Creates a background task to wait for SSH and remove tags.
        Uses the VMTracker to prevent duplicate processing.
        
        Args:
            domain: The libvirt domain that started
        """
        try:
            vm_name = domain.name()
            vm_uuid = domain.UUIDString()
        except libvirt.libvirtError as e:
            logger.error(f"Failed to get VM info: {e}")
            return
        
        # Create a task to monitor this VM (pass name/uuid, not domain object)
        task = asyncio.create_task(
            self._monitor_vm(vm_uuid, vm_name)
        )
        
        # Register with tracker (debouncing)
        started = await self.vm_tracker.start_monitoring(vm_uuid, vm_name, task)
        
        if not started:
            # Already being monitored - cancel this task
            task.cancel()
    
    async def _get_vm_ip_with_retry(
        self,
        vm_name: str,
        max_attempts: int = 10,
        retry_interval: int = 3
    ) -> Optional[str]:
        """
        Get VM IP address with retry logic.
        
        VMs may not have an IP immediately after starting (DHCP lease renewal).
        This retries a few times before giving up.
        
        Args:
            vm_name: VM name
            max_attempts: Maximum number of attempts (default: 10)
            retry_interval: Seconds between attempts (default: 3)
            
        Returns:
            IP address string, or None if not found after all attempts
        """
        for attempt in range(1, max_attempts + 1):
            try:
                # Look up domain fresh (thread-safe)
                loop = asyncio.get_running_loop()
                
                def get_ip_for_vm():
                    """Helper to look up domain and get IP in executor thread"""
                    try:
                        domain = self.conn.lookupByName(vm_name)
                        return get_vm_ip(domain, None)
                    except libvirt.libvirtError as e:
                        raise VMNotFoundException(f"VM {vm_name} not found: {e}")
                
                ip_address = await loop.run_in_executor(None, get_ip_for_vm)
                
                if ip_address and not ip_address.startswith("127."):
                    logger.info(
                        f"Got IP address {ip_address} for VM {vm_name} "
                        f"(attempt {attempt}/{max_attempts})"
                    )
                    return ip_address
                
                if ip_address and ip_address.startswith("127."):
                    logger.debug(
                        f"VM {vm_name} returned loopback address {ip_address}, "
                        f"retrying in {retry_interval}s "
                        f"(attempt {attempt}/{max_attempts})"
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(retry_interval)
                    continue
                
                if attempt < max_attempts:
                    logger.debug(
                        f"VM {vm_name} has no IP yet, retrying in {retry_interval}s "
                        f"(attempt {attempt}/{max_attempts})"
                    )
                    await asyncio.sleep(retry_interval)
                else:
                    logger.warning(
                        f"VM {vm_name} has no IP address after {max_attempts} "
                        f"attempts over {max_attempts * retry_interval}s"
                    )
                    return None
            
            except VMNotFoundException as e:
                logger.error(f"Could not get IP for VM {vm_name}: {e}")
                return None
            except Exception as e:
                logger.error(
                    f"Unexpected error getting IP for VM {vm_name} "
                    f"(attempt {attempt}): {e}"
                )
                if attempt < max_attempts:
                    await asyncio.sleep(retry_interval)
                else:
                    return None
        
        return None
    
    async def _monitor_vm(
        self,
        vm_uuid: str,
        vm_name: str
    ) -> None:
        """
        Monitor a VM until SSH becomes available, then remove tags.
        
        Args:
            vm_uuid: VM UUID
            vm_name: VM name
        """
        try:
            logger.info(f"Starting monitoring for VM {vm_name} (uuid={vm_uuid})")
            
            # Get the VM's IP address (with retry logic)
            ip_address = await self._get_vm_ip_with_retry(vm_name)
            
            if not ip_address:
                logger.warning(f"VM {vm_name} has no IP address, cannot check SSH")
                return
            
            logger.info(f"VM {vm_name} has IP address {ip_address}")
            
            # Wait for SSH to become available
            ssh_result = await self.ssh_checker.wait_for_ssh(ip_address, vm_name)
            
            if ssh_result == "timeout":
                logger.warning(
                    f"SSH check timed out for VM {vm_name} ({ip_address}), "
                    "VM may be broken"
                )
                await self._mark_vm_broken(vm_name, vm_uuid)
                await self._run_on_broken_script(vm_name, vm_uuid, ip_address)
                return
            
            if ssh_result != "success":
                logger.warning(
                    f"SSH check failed for VM {vm_name} ({ip_address}): "
                    f"{ssh_result}, not removing tags"
                )
                return
            
            # SSH succeeded and fresh boot confirmed
            # Wait a few seconds to ensure ansible-deployer has fully exited
            # (ansible-deployer's reset_vm() is non-blocking - it initiates reboot
            # and immediately calls mark_available(), so metadata is cleared before
            # the VM finishes rebooting. By waiting here, we ensure the ansible-deployer
            # process has completed its cleanup and exited.)
            logger.debug(
                f"Waiting 5 seconds before removing tags from {vm_name} "
                "to ensure ansible-deployer cleanup is complete"
            )
            await asyncio.sleep(5)
            
            # Check if VM is still actively in use by ansible-deployer.
            # A playbook may reboot the VM mid-run (e.g., OS install), which
            # triggers vm-manager to start monitoring. But the deployer is still
            # orchestrating the VM - removing the 'used' tag now would allow
            # another deployer to allocate it concurrently.
            if await self._is_vm_in_use(vm_name):
                logger.info(
                    f"VM {vm_name} is still actively in use by ansible-deployer, "
                    "skipping tag removal"
                )
                return
            
            # Now remove tags
            await self._remove_tags(vm_name, vm_uuid)
        
        except asyncio.CancelledError:
            logger.info(f"Monitoring cancelled for VM {vm_name}")
            raise
        
        except Exception as e:
            logger.error(
                f"Unexpected error monitoring VM {vm_name}: {e}",
                exc_info=True
            )
        
        finally:
            # Always stop tracking this VM when done
            await self.vm_tracker.stop_monitoring(vm_uuid)
    

    async def _is_vm_in_use(self, vm_name: str) -> bool:
        """
        Check if a VM is actively in use by ansible-deployer.
        
        Reads the VM's metadata to check the in_use flag. This prevents
        removing the 'used' tag while a deployer session is still active
        (e.g., the playbook rebooted the VM mid-run).
        
        Args:
            vm_name: VM name
            
        Returns:
            True if VM has in_use=true in metadata, False otherwise
        """
        loop = asyncio.get_running_loop()
        
        try:
            def check_in_use():
                """Helper to check metadata in executor thread"""
                try:
                    domain = self.conn.lookupByName(vm_name)
                    metadata_mgr = MetadataManager(domain)
                    return metadata_mgr.is_in_use()
                except libvirt.libvirtError as e:
                    logger.warning(f"Failed to check in_use for {vm_name}: {e}")
                    return False
            
            return await loop.run_in_executor(None, check_in_use)
        
        except Exception as e:
            logger.warning(f"Error checking in_use for {vm_name}: {e}")
            # On error, assume not in use to avoid blocking tag removal forever
            return False

    async def _mark_vm_broken(
        self,
        vm_name: str,
        vm_uuid: str
    ) -> None:
        """
        Mark a VM as broken by adding the broken tag.
        
        Called when SSH times out after max_wait_time. The 'used' tag is
        intentionally NOT removed so the VM stays reserved and won't be
        reallocated by ansible-deployer.
        
        Args:
            vm_name: VM name
            vm_uuid: VM UUID (for logging)
        """
        if not self.broken_tag:
            logger.info(
                f"VM {vm_name} SSH timed out but no broken tag configured, "
                "VM will keep its current tags"
            )
            return
        
        loop = asyncio.get_running_loop()
        
        try:
            logger.warning(
                f"Marking VM {vm_name} as broken (adding tag '{self.broken_tag}'). "
                f"The 'used' tag is intentionally kept so the VM won't be reallocated."
            )
            
            def add_broken_tag():
                """Helper to look up domain and add broken tag in executor thread"""
                try:
                    domain = self.conn.lookupByName(vm_name)
                    add_vm_tag(self.conn, domain, self.broken_tag)
                except libvirt.libvirtError as e:
                    raise Exception(f"Failed to add broken tag: {e}")
            
            await loop.run_in_executor(None, add_broken_tag)
            
            logger.warning(
                f"Successfully marked VM {vm_name} as broken "
                f"(tag '{self.broken_tag}' added)"
            )
        
        except Exception as e:
            logger.error(
                f"Failed to mark VM {vm_name} as broken: {e}",
                exc_info=True
            )

    async def _run_on_broken_script(
        self,
        vm_name: str,
        vm_uuid: str,
        ip_address: Optional[str] = None
    ) -> None:
        """
        Run the external on-broken script with VM information as environment variables.
        
        The script is called asynchronously (fire-and-forget). Non-zero exit codes
        are logged as warnings but do not affect vm-manager operation.
        
        Environment variables passed to the script:
            VM_NAME: VM name
            VM_UUID: Libvirt UUID
            VM_IP: Last known IP address (empty if unavailable)
            VM_TAGS: Comma-separated list of current tags
            VM_BROKEN_TAG: The broken tag that was added
            VM_WAIT_TIME: Max wait time in seconds
            LIBVIRT_URI: Libvirt connection URI
        
        Args:
            vm_name: VM name
            vm_uuid: VM UUID
            ip_address: Last known IP address (may be None)
        """
        if not self.on_broken:
            return
        
        try:
            # Gather VM tags
            loop = asyncio.get_running_loop()
            
            def get_tags():
                try:
                    domain = self.conn.lookupByName(vm_name)
                    return get_vm_tags(domain)
                except Exception:
                    return []
            
            vm_tags = await loop.run_in_executor(None, get_tags)
            
            # Build environment for the script
            env = os.environ.copy()
            env.update({
                "VM_NAME": vm_name,
                "VM_UUID": vm_uuid,
                "VM_IP": ip_address or "",
                "VM_TAGS": ",".join(vm_tags),
                "VM_BROKEN_TAG": self.broken_tag or "",
                "VM_WAIT_TIME": str(self.ssh_checker.max_wait_time or ""),
                "LIBVIRT_URI": self.libvirt_uri,
            })
            
            logger.info(
                f"Running on-broken script '{self.on_broken}' for VM {vm_name}"
            )
            
            # Run the script asynchronously with a 60-second timeout
            process = await asyncio.create_subprocess_exec(
                self.on_broken,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=60
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"On-broken script '{self.on_broken}' for VM {vm_name} "
                    "timed out after 60 seconds, killing process"
                )
                process.kill()
                await process.wait()
                return
            
            if stdout:
                logger.debug(
                    f"On-broken script stdout for {vm_name}: "
                    f"{stdout.decode('utf-8', errors='replace').strip()}"
                )
            if stderr:
                logger.debug(
                    f"On-broken script stderr for {vm_name}: "
                    f"{stderr.decode('utf-8', errors='replace').strip()}"
                )
            
            if process.returncode != 0:
                logger.warning(
                    f"On-broken script '{self.on_broken}' for VM {vm_name} "
                    f"exited with code {process.returncode}"
                )
            else:
                logger.info(
                    f"On-broken script completed successfully for VM {vm_name}"
                )
        
        except Exception as e:
            logger.error(
                f"Failed to run on-broken script for VM {vm_name}: {e}",
                exc_info=True
            )

    async def remove_stale_tags(self, domain: libvirt.virDomain) -> None:
        """
        Remove stale tags from a VM directly, without waiting for SSH.
        
        Used by the stale tag scan and --check-existing startup scan for VMs
        that have removable tags but are not actively in use (in_use=false or
        no metadata). These VMs don't need an SSH check — the deploy already
        finished.
        
        Args:
            domain: The libvirt domain to clean
        """
        try:
            vm_name = domain.name()
            vm_uuid = domain.UUIDString()
        except libvirt.libvirtError as e:
            logger.error(f"Failed to get VM info for stale tag removal: {e}")
            return
        
        await self._remove_tags(vm_name, vm_uuid)
    
    async def _remove_tags(
        self,
        vm_name: str,
        vm_uuid: str
    ) -> None:
        """
        Remove the configured tags from a VM.
        
        Args:
            vm_name: VM name
            vm_uuid: VM UUID (for logging)
        """
        # Use run_in_executor to run libvirt calls in thread pool
        # (libvirt is synchronous, but we're in an async context)
        loop = asyncio.get_running_loop()
        
        for tag in self.tags_to_remove:
            try:
                logger.info(f"Removing tag '{tag}' from VM {vm_name}")
                
                def remove_tag_for_vm():
                    """Helper to look up domain and remove tag in executor thread"""
                    try:
                        domain = self.conn.lookupByName(vm_name)
                        remove_vm_tag(self.conn, domain, tag)
                    except libvirt.libvirtError as e:
                        raise Exception(f"Failed to remove tag: {e}")
                
                # Run in thread pool to avoid blocking
                await loop.run_in_executor(None, remove_tag_for_vm)
                
                logger.info(f"Successfully removed tag '{tag}' from VM {vm_name}")
            
            except Exception as e:
                logger.error(
                    f"Failed to remove tag '{tag}' from VM {vm_name}: {e}",
                    exc_info=True
                )
