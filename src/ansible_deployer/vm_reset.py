"""
VM reset functionality for cleaning and rebooting VMs.
"""
import libvirt
import libvirt_qemu
import logging
import time
import json
from typing import Optional


logger = logging.getLogger(__name__)


class VMResetError(Exception):
    """Raised when VM reset fails."""
    pass


class VMResetManager:
    """Manages VM reset operations.
    
    Note: Reset operations are non-blocking. VMs are wiped and reboot is
    initiated, but we don't wait for them to come back up.
    """

    def __init__(self):
        """Initialize VM reset manager."""
        pass

    def reset_vm(self, domain: libvirt.virDomain) -> None:
        """Reset VM by wiping disk and rebooting.
        
        This executes 'wipefs -af /dev/vda' inside the VM and then reboots it.
        Requires QEMU guest agent with guest-exec command enabled.
        
        If guest-exec is disabled in the VM, the reset is skipped entirely
        and instructions are logged on how to enable it.
        
        Args:
            domain: libvirt domain object
            
        Raises:
            VMResetError: If reset operation fails
        """
        vm_name = domain.name()
        logger.info(f"Starting reset for VM: {vm_name}")

        try:
            # Check if guest agent is available
            if not self._is_agent_available(domain):
                logger.warning(
                    f"QEMU guest agent not available in {vm_name}. "
                    f"Skipping VM reset. Install qemu-guest-agent in the VM."
                )
                return
            
            # Check if guest-exec command is enabled
            if not self._check_guest_exec_available(domain):
                logger.info(
                    f"VM {vm_name}: guest-exec command is disabled. Skipping reset.\n"
                    f"  To enable full VM reset (disk wipe + reboot), configure the guest agent:\n"
                    f"  1. SSH into VM: {vm_name}\n"
                    f"  2. Edit: /etc/sysconfig/qemu-ga (RHEL/CentOS) or /etc/default/qemu-guest-agent (Debian/Ubuntu)\n"
                    f"  3. Remove 'guest-exec' from the blacklist, or add: BLACKLIST=\n"
                    f"  4. Restart agent: systemctl restart qemu-guest-agent\n"
                    f"  Note: Enabling guest-exec reduces security isolation. Only enable on trusted VMs."
                )
                return
            
            # Execute wipefs command via qemu-agent
            self._execute_command(domain, "wipefs -af /dev/vda")
            logger.info(f"Wipefs completed for {vm_name}")

            # Sync filesystem before reboot
            self._execute_command(domain, "sync")
            
            # Reboot the VM
            self._reboot_vm(domain)
            logger.info(f"VM {vm_name} reset completed")

        except Exception as e:
            logger.error(f"Failed to reset VM {vm_name}: {e}")
            raise VMResetError(f"Failed to reset VM {vm_name}: {e}") from e

    def _check_guest_exec_available(self, domain: libvirt.virDomain) -> bool:
        """Check if guest-exec command is enabled in the VM.
        
        Args:
            domain: libvirt domain object
            
        Returns:
            True if guest-exec is available and enabled
        """
        try:
            # Try to execute a harmless command
            cmd_json = '{"execute":"guest-exec", "arguments":{"path":"/bin/true", "arg":[], "capture-output":false}}'
            libvirt_qemu.qemuAgentCommand(domain, cmd_json, 5, 0)
            return True
        except libvirt.libvirtError as e:
            error_msg = str(e)
            if 'guest-exec' in error_msg and 'disabled' in error_msg.lower():
                # guest-exec is explicitly disabled
                return False
            # Other errors might be transient or different issues
            logger.debug(f"guest-exec check failed: {e}")
            return False
    
    def _execute_command(
        self,
        domain: libvirt.virDomain,
        command: str,
    ) -> str:
        """Execute command in VM via qemu-agent.
        
        Uses libvirt_qemu.qemuAgentCommand() to send commands to the
        QEMU guest agent running inside the VM.
        
        Args:
            domain: libvirt domain object
            command: Shell command to execute
            
        Returns:
            Command output (JSON string from guest agent)
            
        Raises:
            VMResetError: If command execution fails
        """
        try:
            # Execute command via qemu-agent using libvirt_qemu module
            cmd_json = f'{{"execute":"guest-exec", "arguments":{{"path":"/bin/sh", "arg":["-c", "{command}"], "capture-output":true}}}}'
            
            result = libvirt_qemu.qemuAgentCommand(domain, cmd_json, 30, 0)
            logger.debug(f"Command result: {result}")
            
            return result

        except libvirt.libvirtError as e:
            logger.error(f"Failed to execute command via qemu-agent: {e}")
            raise VMResetError(f"Failed to execute command: {e}") from e

    def _is_agent_available(self, domain: libvirt.virDomain) -> bool:
        """Check if QEMU guest agent is available and responding.
        
        Args:
            domain: libvirt domain object
            
        Returns:
            True if agent is available and responding
        """
        try:
            # Try to ping the agent using libvirt_qemu module
            libvirt_qemu.qemuAgentCommand(domain, '{"execute":"guest-ping"}', 5, 0)
            return True
        except libvirt.libvirtError:
            return False

    def _reboot_vm(self, domain: libvirt.virDomain) -> None:
        """Reboot the VM.
        
        Note: This initiates the reboot but does NOT wait for the VM to come
        back up. The VM will be available for the next deployment whenever
        it finishes rebooting.
        
        Args:
            domain: libvirt domain object
        """
        try:
            # Try graceful reboot via agent first
            try:
                domain.reboot(libvirt.VIR_DOMAIN_REBOOT_GUEST_AGENT)
                logger.info("Initiated graceful reboot via guest agent (non-blocking)")
            except libvirt.libvirtError:
                # Fall back to ACPI reboot
                logger.warning("Guest agent reboot failed, using ACPI")
                domain.reboot(libvirt.VIR_DOMAIN_REBOOT_ACPI_POWER_BTN)
                logger.info("Initiated ACPI reboot (non-blocking)")
            
            # Note: We do NOT wait for the VM to come back up
            # The VM will be ready for the next deployment when it boots

        except libvirt.libvirtError as e:
            raise VMResetError(f"Failed to initiate VM reboot: {e}") from e

    def shutdown_vm(self, domain: libvirt.virDomain, force: bool = False) -> None:
        """Shutdown a VM.
        
        Args:
            domain: libvirt domain object
            force: Force shutdown if graceful fails
        """
        vm_name = domain.name()
        try:
            if force:
                logger.info(f"Force destroying VM: {vm_name}")
                domain.destroy()
            else:
                logger.info(f"Gracefully shutting down VM: {vm_name}")
                domain.shutdown()
                
                # Wait for shutdown
                start_time = time.time()
                while time.time() - start_time < 60:
                    state = domain.state()[0]
                    if state == libvirt.VIR_DOMAIN_SHUTOFF:
                        logger.info(f"VM {vm_name} shut down successfully")
                        return
                    time.sleep(2)
                
                # Timeout, force it
                logger.warning(f"Graceful shutdown timeout, forcing VM {vm_name}")
                domain.destroy()

        except libvirt.libvirtError as e:
            raise VMResetError(f"Failed to shutdown VM {vm_name}: {e}") from e