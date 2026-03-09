"""
Tag cleanup orchestration logic.

Coordinates SSH checking and tag removal for VMs using a two-phase
timeout to separate broken VM detection from repair.
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import List, Optional, Tuple
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

    Uses a two-phase timeout to separate broken VM detection from repair:

    Phase 1 (broken_timeout): Wait for SSH to become available.
        - Success → remove 'used' tag
        - Timeout → add 'broken' tag, proceed to Phase 2

    Phase 2 (on_broken_delay or indefinite):
        - If on-broken script configured: wait on_broken_delay, then run script
        - If broken_tag set but no script: monitor SSH indefinitely
        - SSH recovery at any point → remove broken + used tags
    """

    def __init__(
        self,
        conn: libvirt.virConnect,
        ssh_checker: SSHChecker,
        vm_tracker: VMTracker,
        tags_to_remove: List[str],
        broken_tag: Optional[str] = None,
        broken_timeout: int = 300,
        on_broken_delay: int = 1500,
        on_broken: Optional[str] = None,
        on_broken_timeout: int = 300,
        on_broken_retries: Optional[int] = None,
        on_broken_retry_delay: int = 60,
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
            broken_timeout: Seconds of SSH failure before adding broken tag
                (default: 300). Phase 1 of two-phase timeout.
            on_broken_delay: Seconds after broken tag before running on-broken
                script (default: 1500). Phase 2 of two-phase timeout. SSH
                monitoring continues during this delay.
            on_broken: Path to external script to run when a VM is marked
                broken (None = disabled)
            on_broken_timeout: Seconds before killing the on-broken script
                (default: 300)
            on_broken_retries: Max retries for on-broken script
                (None = unlimited)
            on_broken_retry_delay: Seconds between on-broken retries
                (default: 60)
            libvirt_uri: Libvirt connection URI (passed to on_broken script
                as env var)
        """
        self.conn = conn
        self.ssh_checker = ssh_checker
        self.vm_tracker = vm_tracker
        self.tags_to_remove = tags_to_remove
        self.broken_tag = broken_tag
        self.broken_timeout = broken_timeout
        self.on_broken_delay = on_broken_delay
        self.on_broken = on_broken
        self.on_broken_timeout = on_broken_timeout
        self.on_broken_retries = on_broken_retries
        self.on_broken_retry_delay = on_broken_retry_delay
        self.libvirt_uri = libvirt_uri

    async def handle_vm_started(
        self,
        domain: libvirt.virDomain,
        is_recovery: bool = False
    ) -> None:
        """
        Handle a VM start event.

        Creates a background task to wait for SSH and remove tags.
        Uses the VMTracker to prevent duplicate processing.

        Args:
            domain: The libvirt domain that started
            is_recovery: True if this is a broken VM recovery session.
                Affects _monitor_vm behavior: changes log messages to
                say "recovery" and skips _mark_vm_broken on Phase 1
                timeout (broken tag is already present).
        """
        try:
            vm_name = domain.name()
            vm_uuid = domain.UUIDString()
        except libvirt.libvirtError as e:
            logger.error(f"Failed to get VM info: {e}")
            return

        # Create a task to monitor this VM (pass name/uuid, not domain object)
        task = asyncio.create_task(
            self._monitor_vm(vm_uuid, vm_name, is_recovery=is_recovery)
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

                if ip_address:
                    logger.info(
                        f"Got IP address {ip_address} for VM {vm_name} "
                        f"(attempt {attempt}/{max_attempts})"
                    )
                    return ip_address

                # No valid IP yet (loopback addresses are filtered by get_vm_ip)
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

    async def _wait_for_vm_ssh(
        self,
        vm_name: str,
        timeout: Optional[float],
        existing_ip: Optional[str] = None
    ) -> Tuple[Optional[str], str]:
        """
        Resolve VM IP and wait for SSH within the given timeout.

        Combines IP resolution and SSH waiting into a single operation
        with a shared time budget. If an existing IP is provided, IP
        resolution is skipped.

        Args:
            vm_name: VM name
            timeout: Maximum seconds to wait (None = wait indefinitely)
            existing_ip: Previously resolved IP address (skip IP resolution)

        Returns:
            Tuple of (ip_address, result) where result is one of:
            - "success": SSH connected and fresh boot confirmed
            - "timeout": Timed out waiting for IP or SSH
            - "auth_failure": SSH authentication failed (config error)
            ip_address may be None if IP was never resolved.
        """
        start_time = datetime.now()
        ip_address = existing_ip
        ip_attempt_round = 0

        # IP resolution loop (skip if we already have an IP)
        while ip_address is None:
            ip_attempt_round += 1
            ip_address = await self._get_vm_ip_with_retry(vm_name)

            if ip_address:
                logger.info(f"VM {vm_name} has IP address {ip_address}")
                break

            # Check timeout
            if timeout is not None:
                elapsed = (datetime.now() - start_time).total_seconds()
                if elapsed >= timeout:
                    logger.warning(
                        f"VM {vm_name} has no IP address after {elapsed:.1f}s "
                        f"({ip_attempt_round} rounds of IP resolution)"
                    )
                    return (None, "timeout")

            logger.info(
                f"VM {vm_name} has no IP address, will retry in "
                f"{self.ssh_checker.check_interval}s "
                f"(round {ip_attempt_round})"
            )
            await asyncio.sleep(self.ssh_checker.check_interval)

        # SSH wait with remaining timeout budget
        remaining = None
        if timeout is not None:
            elapsed = (datetime.now() - start_time).total_seconds()
            remaining = max(0, timeout - elapsed)

        result = await self.ssh_checker.wait_for_ssh(
            ip_address, vm_name, max_wait_time_override=remaining
        )
        return (ip_address, result)

    async def _monitor_vm(
        self,
        vm_uuid: str,
        vm_name: str,
        is_recovery: bool = False
    ) -> None:
        """
        Monitor a VM through a two-phase timeout until SSH becomes available.

        Phase 1 (broken_timeout seconds):
            Wait for VM IP and SSH connectivity. If SSH succeeds, remove
            the 'used' tag. If the timeout expires, add the 'broken' tag
            and proceed to Phase 2.

        Phase 2 (on_broken_delay seconds, or indefinite if no script):
            Continue monitoring SSH. If SSH recovers, remove both broken
            and used tags. If the timeout expires and an on-broken script
            is configured, run the script.

        Args:
            vm_uuid: VM UUID
            vm_name: VM name
            is_recovery: True if this is a broken VM recovery session
        """
        tracking_stopped = False
        monitor_type = "recovery" if is_recovery else "normal"
        try:
            logger.info(
                f"Starting {monitor_type} monitoring for VM {vm_name} "
                f"(uuid={vm_uuid}), "
                f"broken_timeout={self.broken_timeout}s, "
                f"on_broken_delay={self.on_broken_delay}s"
            )

            # ── Phase 1: Wait for SSH within broken_timeout ──────────
            ip, result = await self._wait_for_vm_ssh(
                vm_name, self.broken_timeout
            )

            if result == "success":
                logger.info(
                    f"VM {vm_name} SSH succeeded (Phase 1), "
                    "checking in-use status"
                )
                # Remove broken tag if present (e.g., re-monitoring after
                # a repair script where the broken tag was intentionally
                # kept until SSH proved the VM is healthy).
                await self._remove_broken_tag(vm_name, vm_uuid)
                # Wait a few seconds to ensure ansible-deployer has fully
                # exited (ansible-deployer's reset_vm() is non-blocking —
                # it initiates reboot and immediately calls mark_available(),
                # so metadata is cleared before the VM finishes rebooting).
                await asyncio.sleep(5)
                if await self._is_vm_in_use(vm_name):
                    logger.info(
                        f"VM {vm_name} is still actively in use by "
                        "ansible-deployer, skipping tag removal"
                    )
                    return
                await self._remove_tags(vm_name, vm_uuid)
                return

            if result == "auth_failure":
                logger.warning(
                    f"SSH authentication failed for VM {vm_name}, "
                    "not removing tags"
                )
                return

            # ── Phase 1 timed out → mark VM as broken ───────────────
            if is_recovery:
                logger.warning(
                    f"VM {vm_name} SSH timed out after "
                    f"{self.broken_timeout}s (Phase 1), "
                    "broken tag already present (recovery session)"
                )
            else:
                logger.warning(
                    f"VM {vm_name} SSH timed out after "
                    f"{self.broken_timeout}s (Phase 1), "
                    "marking as broken"
                )
                await self._mark_vm_broken(vm_name, vm_uuid)

            # Keep the tracker slot occupied during Phase 2. This prevents
            # event handlers and stale scans from starting concurrent
            # monitoring sessions for the same VM. The slot is freed in
            # the finally block, or explicitly by _handle_successful_repair
            # before starting fresh monitoring.

            # ── Phase 2: Continue monitoring SSH ─────────────────────
            if self.on_broken:
                phase2_timeout = self.on_broken_delay
                logger.info(
                    f"Phase 2: monitoring VM {vm_name} SSH for "
                    f"{self.on_broken_delay}s before running on-broken script"
                )
            elif self.broken_tag:
                phase2_timeout = None
                logger.info(
                    f"Phase 2: monitoring VM {vm_name} SSH indefinitely "
                    "(no on-broken script configured)"
                )
            else:
                # No script and no broken tag → nothing more to do
                return

            ip2, result2 = await self._wait_for_vm_ssh(
                vm_name, phase2_timeout, existing_ip=ip
            )

            if result2 == "success":
                logger.info(
                    f"VM {vm_name} recovered during Phase 2, "
                    "removing broken and used tags"
                )
                await self._remove_broken_tag(vm_name, vm_uuid)
                await asyncio.sleep(5)
                if await self._is_vm_in_use(vm_name):
                    logger.info(
                        f"VM {vm_name} is still actively in use, "
                        "skipping used tag removal "
                        "(broken tag already removed)"
                    )
                    return
                await self._remove_tags(vm_name, vm_uuid)
                return

            if result2 == "timeout" and self.on_broken:
                total_wait = self.broken_timeout + self.on_broken_delay
                logger.warning(
                    f"VM {vm_name} SSH still failing after "
                    f"{total_wait}s total, running on-broken script"
                )
                script_ok = await self._run_on_broken_script(
                    vm_name, vm_uuid, ip2 or ip
                )
                if script_ok:
                    await self._handle_successful_repair(vm_name, vm_uuid)
                    # Repair freed the tracker and started fresh monitoring.
                    # Prevent the finally block from freeing the new session.
                    #
                    # Note: if _handle_successful_repair raised CancelledError,
                    # we never reach here — tracking_stopped stays False and
                    # the finally block calls stop_monitoring (which is safe
                    # even if _handle_successful_repair already called it,
                    # because stop_monitoring is idempotent).
                    tracking_stopped = True
                return

            # auth_failure or unexpected result in Phase 2
            if result2 != "timeout":
                logger.warning(
                    f"SSH check for VM {vm_name} returned '{result2}' "
                    "during Phase 2, giving up"
                )

        except asyncio.CancelledError:
            logger.info(f"Monitoring cancelled for VM {vm_name}")
            raise

        except Exception as e:
            logger.error(
                f"Unexpected error monitoring VM {vm_name}: {e}",
                exc_info=True
            )

        finally:
            # Free the tracker slot unless _handle_successful_repair
            # already freed it and started a fresh monitoring session
            # (in which case tracking_stopped is True).
            if not tracking_stopped:
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

    async def _handle_successful_repair(
        self,
        vm_name: str,
        vm_uuid: str
    ) -> None:
        """
        Handle post-repair actions after an on-broken script succeeds.

        Frees the tracker slot and triggers a fresh monitoring session
        while keeping the broken tag. The broken tag is only removed
        when SSH actually succeeds during re-monitoring — a successful
        script exit code alone does not prove the VM is healthy.

        The on-broken script typically restarts the VM (e.g.,
        reset-vm-disks.sh), so by this point the VM is running with fresh
        disks. Re-monitoring will wait for SSH and then remove the broken
        and used tags.

        If re-monitoring cannot be triggered (e.g., VM no longer exists),
        the stale tag scan will eventually clean up.

        Args:
            vm_name: VM name
            vm_uuid: VM UUID
        """
        try:
            # Free the tracker slot so handle_vm_started can register
            # the fresh monitoring session.
            await self.vm_tracker.stop_monitoring(vm_uuid)

            loop = asyncio.get_running_loop()
            domain = await loop.run_in_executor(
                None, self.conn.lookupByName, vm_name
            )
            logger.info(
                f"Triggering re-monitoring for VM {vm_name} "
                "(broken tag kept until SSH succeeds)"
            )
            await self.handle_vm_started(domain, is_recovery=True)
        except Exception as e:
            logger.warning(
                f"Could not trigger re-monitoring for {vm_name} after "
                f"repair: {e}. The stale tag scan will eventually clean up."
            )

    async def _remove_broken_tag(
        self,
        vm_name: str,
        vm_uuid: str
    ) -> None:
        """
        Remove the broken tag from a VM after SSH recovery.

        Called when SSH succeeds (Phase 1 or Phase 2), proving the VM
        is actually healthy. This is a no-op if no broken_tag is
        configured or the tag is not present.

        Args:
            vm_name: VM name
            vm_uuid: VM UUID (for logging)
        """
        if not self.broken_tag:
            return

        loop = asyncio.get_running_loop()

        try:
            logger.info(
                f"Removing broken tag '{self.broken_tag}' from VM "
                f"{vm_name} (uuid={vm_uuid})"
            )

            def do_remove_broken_tag():
                """Helper to look up domain and remove broken tag in executor thread"""
                try:
                    domain = self.conn.lookupByName(vm_name)
                    remove_vm_tag(self.conn, domain, self.broken_tag)
                except libvirt.libvirtError as e:
                    raise Exception(f"Failed to remove broken tag: {e}") from e

            await loop.run_in_executor(None, do_remove_broken_tag)

            logger.info(
                f"Successfully removed broken tag '{self.broken_tag}' "
                f"from VM {vm_name}"
            )

        except Exception as e:
            logger.error(
                f"Failed to remove broken tag from VM {vm_name}: {e}",
                exc_info=True
            )

    async def _mark_vm_broken(
        self,
        vm_name: str,
        vm_uuid: str
    ) -> None:
        """
        Mark a VM as broken by adding the broken tag.

        Called when SSH times out after broken_timeout (Phase 1). The 'used'
        tag is intentionally NOT removed so the VM stays reserved and won't
        be reallocated by ansible-deployer.

        Args:
            vm_name: VM name
            vm_uuid: VM UUID
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
                    raise Exception(f"Failed to add broken tag: {e}") from e

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
    ) -> bool:
        """
        Run the external on-broken script with VM information as environment variables.

        Retries on failure (non-zero exit or timeout) according to on_broken_retries
        and on_broken_retry_delay. If on_broken_retries is None, retries forever.

        Environment variables passed to the script:
            VM_NAME: VM name
            VM_UUID: Libvirt UUID
            VM_IP: Last known IP address (empty if unavailable)
            VM_TAGS: Comma-separated list of current tags
            VM_BROKEN_TAG: The broken tag that was added
            VM_WAIT_TIME: Total wait time before script (broken_timeout + on_broken_delay)
            LIBVIRT_URI: Libvirt connection URI

        Args:
            vm_name: VM name
            vm_uuid: VM UUID
            ip_address: Last known IP address (may be None)

        Returns:
            True if script succeeded, False if no script configured or
            retries exhausted
        """
        if not self.on_broken:
            return False

        try:
            # Gather VM tags
            loop = asyncio.get_running_loop()

            def get_tags():
                try:
                    domain = self.conn.lookupByName(vm_name)
                    return get_vm_tags(domain)
                except Exception as e:
                    logger.debug(
                        f"Could not get tags for VM {vm_name} "
                        f"(for on-broken script env): {e}"
                    )
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
                "VM_WAIT_TIME": str(self.broken_timeout + self.on_broken_delay),
                "LIBVIRT_URI": self.libvirt_uri,
            })

            attempt = 0
            while True:
                attempt += 1
                retry_label = f" (attempt {attempt})" if attempt > 1 else ""

                logger.info(
                    f"Running on-broken script '{self.on_broken}' "
                    f"for VM {vm_name}{retry_label}"
                )

                success = await self._execute_on_broken_script(
                    vm_name, env
                )

                if success:
                    return True

                # Check if we've exhausted retries
                if self.on_broken_retries is not None and attempt >= self.on_broken_retries + 1:
                    logger.error(
                        f"On-broken script for VM {vm_name} failed after "
                        f"{attempt} attempt(s), giving up"
                    )
                    return False

                # Wait before retrying
                retry_desc = ""
                if self.on_broken_retries is not None:
                    remaining = self.on_broken_retries + 1 - attempt
                    retry_desc = f" ({remaining} retries remaining)"

                logger.info(
                    f"Retrying on-broken script for VM {vm_name} "
                    f"in {self.on_broken_retry_delay}s{retry_desc}"
                )
                await asyncio.sleep(self.on_broken_retry_delay)

        except asyncio.CancelledError:
            logger.info(
                f"On-broken script retry loop cancelled for VM {vm_name}"
            )
            raise

        except Exception as e:
            logger.error(
                f"Failed to run on-broken script for VM {vm_name}: {e}",
                exc_info=True
            )
            return False

    async def _execute_on_broken_script(
        self,
        vm_name: str,
        env: dict
    ) -> bool:
        """
        Execute the on-broken script once.

        Args:
            vm_name: VM name (for logging)
            env: Environment variables for the script

        Returns:
            True if script succeeded (exit code 0), False otherwise
        """
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                self.on_broken,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.on_broken_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"On-broken script '{self.on_broken}' for VM {vm_name} "
                    f"timed out after {self.on_broken_timeout} seconds, "
                    "killing process"
                )
                process.kill()
                await process.wait()
                return False

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
                return False

            logger.info(
                f"On-broken script completed successfully for VM {vm_name}"
            )
            return True

        except asyncio.CancelledError:
            # Kill the child process to avoid orphans when the daemon shuts down
            if process is not None and process.returncode is None:
                logger.info(
                    f"Killing on-broken script for VM {vm_name} "
                    "(task cancelled)"
                )
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass
            raise

        except Exception as e:
            logger.error(
                f"Failed to execute on-broken script for VM {vm_name}: {e}",
                exc_info=True
            )
            return False

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
                        raise Exception(f"Failed to remove tag: {e}") from e

                # Run in thread pool to avoid blocking
                await loop.run_in_executor(None, remove_tag_for_vm)

                logger.info(f"Successfully removed tag '{tag}' from VM {vm_name}")

            except Exception as e:
                logger.error(
                    f"Failed to remove tag '{tag}' from VM {vm_name}: {e}",
                    exc_info=True
                )
