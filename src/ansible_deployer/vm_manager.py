"""
VM Manager for libvirt operations with tag-based selection.

This module extends the shared vm_tools_common library with deployment-specific
logic like VM allocation, metadata management, and task claiming.
"""
import libvirt
from typing import List, Optional, Dict

from vm_tools_common import (
    VMNotFoundException,
    NoAvailableVMException,
    get_vm_tags,
    add_vm_tag as vm_ops_add_tag,
    remove_vm_tag as vm_ops_remove_tag,
    get_vm_ip,
    get_network_to_interface_mapping,
    list_vm_interfaces,
    get_state_string,
    vm_matches_tags,
)
from .metadata_manager import MetadataManager

# Tag that vm-manager adds to VMs with SSH timeout. Always excluded from
# allocation to prevent deploying to broken VMs.
_BROKEN_TAG = "broken"


class VMManager:
    """Manages libvirt VMs with tag-based selection and deployment-specific operations."""

    def __init__(self, uri: str = "qemu:///system"):
        """Initialize VM manager.
        
        Args:
            uri: Libvirt connection URI
        """
        self.uri = uri
        self.conn: Optional[libvirt.virConnect] = None

    def connect(self) -> None:
        """Establish connection to libvirt.
        
        Raises:
            RuntimeError: If connection fails with user-friendly error message
        """
        try:
            self.conn = libvirt.open(self.uri)
            if self.conn is None:
                raise RuntimeError(f"Failed to connect to libvirt at {self.uri}")
        except libvirt.libvirtError as e:
            error_msg = str(e)
            
            # Check for polkit/authentication errors
            if "polkit" in error_msg.lower() or "authentication" in error_msg.lower():
                raise RuntimeError(
                    "Libvirt authentication failed. This tool requires system-level access to libvirt.\n"
                    "\n"
                    "Solutions:\n"
                    "  1. Run with sudo: sudo ansible-deployer ...\n"
                    "  2. Add your user to the 'libvirt' group: sudo usermod -aG libvirt $USER\n"
                    "     (Then log out and back in for group changes to take effect)\n"
                    "  3. Configure polkit to allow your user access to libvirt\n"
                    "\n"
                    f"Original error: {error_msg}"
                ) from e
            
            # Check for connection refused errors
            elif "refused" in error_msg.lower() or "failed to connect" in error_msg.lower():
                raise RuntimeError(
                    f"Failed to connect to libvirt at {self.uri}\n"
                    "\n"
                    "Possible causes:\n"
                    "  - libvirtd service is not running: sudo systemctl start libvirtd\n"
                    "  - Incorrect URI (check --config or LIBVIRT_DEFAULT_URI)\n"
                    "  - Firewall blocking connection\n"
                    "\n"
                    f"Original error: {error_msg}"
                ) from e
            
            # Generic libvirt error
            else:
                raise RuntimeError(
                    f"Failed to connect to libvirt at {self.uri}\n"
                    f"Error: {error_msg}"
                ) from e

    def disconnect(self) -> None:
        """Close libvirt connection."""
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "VMManager":
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type: type, exc_val: Exception, exc_tb: object) -> None:
        """Context manager exit."""
        self.disconnect()

    def list_vms(self) -> List[Dict[str, str]]:
        """List all VMs with their basic information.
        
        Returns:
            List of VM information dictionaries
        """
        if self.conn is None:
            raise RuntimeError("Not connected to libvirt")

        vms = []
        for domain in self.conn.listAllDomains():
            metadata_mgr = MetadataManager(domain)
            tags = get_vm_tags(domain)
            vms.append({
                "name": domain.name(),
                "uuid": domain.UUIDString(),
                "state": get_state_string(domain.state()[0]),
                "tags": tags,
                "in_use": metadata_mgr.is_in_use(),
                "task_id": metadata_mgr.get_task_id() or "",
            })
        return vms

    def get_vm_by_name(self, name: str) -> libvirt.virDomain:
        """Get VM by name.
        
        Args:
            name: VM name
            
        Returns:
            libvirt domain object
            
        Raises:
            VMNotFoundException: If VM not found
        """
        if self.conn is None:
            raise RuntimeError("Not connected to libvirt")

        try:
            return self.conn.lookupByName(name)
        except libvirt.libvirtError:
            raise VMNotFoundException(f"VM '{name}' not found")

    def get_vm_tags(self, domain: libvirt.virDomain) -> List[str]:
        """Get tags for a VM from its XML description.
        
        Args:
            domain: libvirt domain object
            
        Returns:
            List of tags
        """
        return get_vm_tags(domain)

    def add_vm_tag(self, domain: libvirt.virDomain, tag: str) -> None:
        """Add a tag to VM's description.
        
        Args:
            domain: libvirt domain object
            tag: Tag to add
        """
        if self.conn is None:
            raise RuntimeError("Not connected to libvirt")
        vm_ops_add_tag(self.conn, domain, tag)

    def remove_vm_tag(self, domain: libvirt.virDomain, tag: str) -> None:
        """Remove a tag from VM's description.
        
        Args:
            domain: libvirt domain object
            tag: Tag to remove
        """
        if self.conn is None:
            raise RuntimeError("Not connected to libvirt")
        vm_ops_remove_tag(self.conn, domain, tag)

    def find_available_vm_by_tags(self, tags: List[str], exclude_tags: Optional[List[str]] = None) -> Optional[libvirt.virDomain]:
        """Find an available VM matching any of the given tags.
        
        Args:
            tags: List of tags to match (VM must have at least one)
            exclude_tags: List of tags to exclude (VM must have none of these).
                          The 'broken' tag is always excluded automatically.
            
        Returns:
            Available VM domain or None
        """
        if self.conn is None:
            raise RuntimeError("Not connected to libvirt")

        exclude_tags = list(exclude_tags) if exclude_tags else []
        if _BROKEN_TAG not in exclude_tags:
            exclude_tags.append(_BROKEN_TAG)

        for domain in self.conn.listAllDomains():
            # Check if VM is running
            state = domain.state()[0]
            if state != libvirt.VIR_DOMAIN_RUNNING:
                continue

            # Check if VM is available
            metadata_mgr = MetadataManager(domain)
            if metadata_mgr.is_in_use():
                continue

            # Check tags using shared logic
            vm_tags = get_vm_tags(domain)
            if vm_matches_tags(vm_tags, tags, exclude_tags):
                return domain

        return None

    def find_available_vms_by_tags(self, tags: List[str], count: int, exclude_tags: Optional[List[str]] = None) -> List[libvirt.virDomain]:
        """Find multiple available VMs matching any of the given tags.
        
        Note: This method only finds VMs without claiming them. Use
        allocate_vms() for race-condition-safe allocation.
        
        Args:
            tags: List of tags to match (VM must have at least one)
            count: Number of VMs to find
            exclude_tags: List of tags to exclude (VM must have none of these).
                          The 'broken' tag is always excluded automatically.
            
        Returns:
            List of available VM domains (may be less than count if not enough available)
        """
        if self.conn is None:
            raise RuntimeError("Not connected to libvirt")

        exclude_tags = list(exclude_tags) if exclude_tags else []
        if _BROKEN_TAG not in exclude_tags:
            exclude_tags.append(_BROKEN_TAG)
        available_vms = []

        for domain in self.conn.listAllDomains():
            if len(available_vms) >= count:
                break

            # Check if VM is running
            state = domain.state()[0]
            if state != libvirt.VIR_DOMAIN_RUNNING:
                continue

            # Check if VM is available
            metadata_mgr = MetadataManager(domain)
            if metadata_mgr.is_in_use():
                continue

            # Check tags using shared logic
            vm_tags = get_vm_tags(domain)
            if vm_matches_tags(vm_tags, tags, exclude_tags):
                available_vms.append(domain)

        return available_vms

    def allocate_vms(
        self, tags: List[str], count: int, task_id: str,
        exclude_tags: Optional[List[str]] = None
    ) -> List[libvirt.virDomain]:
        """Find and atomically claim VMs, preventing race conditions.
        
        For each candidate VM, this method attempts to claim it by writing
        the task_id to metadata, then re-reads the metadata to verify
        ownership. If another instance claimed the VM first, it is skipped.
        
        Args:
            tags: List of tags to match (VM must have at least one)
            count: Number of VMs to allocate
            task_id: Unique task identifier used to claim ownership
            exclude_tags: List of tags to exclude (VM must have none of these).
                          The 'broken' tag is always excluded automatically.
            
        Returns:
            List of successfully claimed VM domains (may be less than count)
        """
        if self.conn is None:
            raise RuntimeError("Not connected to libvirt")

        exclude_tags = list(exclude_tags) if exclude_tags else []
        if _BROKEN_TAG not in exclude_tags:
            exclude_tags.append(_BROKEN_TAG)
        claimed_vms = []

        for domain in self.conn.listAllDomains():
            if len(claimed_vms) >= count:
                break

            # Check if VM is running
            state = domain.state()[0]
            if state != libvirt.VIR_DOMAIN_RUNNING:
                continue

            # Check if VM is already in use
            metadata_mgr = MetadataManager(domain)
            if metadata_mgr.is_in_use():
                continue

            # Check tags using shared logic
            vm_tags = get_vm_tags(domain)
            if not vm_matches_tags(vm_tags, tags, exclude_tags):
                continue

            # Attempt atomic claim
            if metadata_mgr.try_claim(task_id):
                claimed_vms.append(domain)
            # else: another instance claimed it first, skip

        return claimed_vms

    def get_network_to_interface_mapping(self, domain: libvirt.virDomain) -> Dict[str, str]:
        """Get mapping of libvirt network names to interface names.
        
        Args:
            domain: libvirt domain object
            
        Returns:
            Dictionary mapping network names to interface names
        """
        return get_network_to_interface_mapping(domain)
    
    def get_vm_ip(self, domain: libvirt.virDomain, network: Optional[str] = None) -> Optional[str]:
        """Get IP address of a VM.
        
        Args:
            domain: libvirt domain object
            network: Optional libvirt network name
            
        Returns:
            IP address or None
        """
        return get_vm_ip(domain, network)
    
    def list_vm_interfaces(self, domain: libvirt.virDomain) -> Dict[str, Dict[str, List[str]]]:
        """List all network interfaces and their IPs for a VM.
        
        Args:
            domain: libvirt domain object
            
        Returns:
            Dictionary with 'networks' and 'interfaces' keys
        """
        return list_vm_interfaces(domain)

    def reset_vm(self, domain: libvirt.virDomain) -> None:
        """Reset VM by running wipefs and rebooting.
        
        Args:
            domain: libvirt domain object
        """
        # This will be handled by VMResetManager
        pass

    def _get_state_string(self, state: int) -> str:
        """Convert libvirt state constant to string.
        
        Args:
            state: libvirt state constant
            
        Returns:
            State string
        """
        return get_state_string(state)
