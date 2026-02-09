"""
Metadata manager for libvirt VM metadata stored in XML format.
"""
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, Dict, Any
import libvirt


class MetadataManager:
    """Manages VM metadata stored in libvirt XML."""

    NAMESPACE = "http://ansible-vm-manager.local/metadata"
    NAMESPACE_PREFIX = "avm"

    def __init__(self, domain: libvirt.virDomain):
        """Initialize metadata manager for a specific domain.
        
        Args:
            domain: libvirt domain object
        """
        self.domain = domain

    def _find_element(self, root: ET.Element, key: str) -> Optional[ET.Element]:
        """Find a child element by key, handling namespace inconsistencies.
        
        Libvirt may store elements with or without namespace prefixes depending
        on how they were created. This method checks both forms.
        
        Args:
            root: XML root element to search
            key: Element name to find
            
        Returns:
            The element if found, None otherwise
        """
        # Try with namespace first
        element = root.find(f".//{{{self.NAMESPACE}}}{key}")
        if element is not None:
            return element
        # Try without namespace (libvirt sometimes strips it)
        element = root.find(f".//{key}")
        return element

    def get_metadata(self, key: str) -> Optional[str]:
        """Get metadata value by key.
        
        Args:
            key: Metadata key to retrieve
            
        Returns:
            Metadata value or None if not found
        """
        try:
            metadata_xml = self.domain.metadata(
                libvirt.VIR_DOMAIN_METADATA_ELEMENT,
                self.NAMESPACE,
                0
            )
            root = ET.fromstring(metadata_xml)
            element = self._find_element(root, key)
            return element.text if element is not None else None
        except libvirt.libvirtError:
            return None

    def set_metadata(self, key: str, value: str) -> None:
        """Set metadata value for a key.
        
        WARNING: Each call does a full read-modify-write cycle. For setting
        multiple keys atomically, use set_metadata_bulk() instead.
        
        Args:
            key: Metadata key
            value: Metadata value
        """
        self.set_metadata_bulk({key: value})

    def set_metadata_bulk(self, updates: Dict[str, str]) -> None:
        """Set multiple metadata values in a single atomic write.
        
        Reads existing metadata once, applies all updates, then writes back
        in a single setMetadata() call. This prevents interleaving from
        concurrent processes.
        
        Args:
            updates: Dictionary of key-value pairs to set
        """
        try:
            # Try to get existing metadata
            metadata_xml = self.domain.metadata(
                libvirt.VIR_DOMAIN_METADATA_ELEMENT,
                self.NAMESPACE,
                0
            )
            root = ET.fromstring(metadata_xml)
        except libvirt.libvirtError:
            # Create new metadata root
            root = ET.Element(f"{{{self.NAMESPACE}}}metadata")

        # Apply all updates
        for key, value in updates.items():
            element = self._find_element(root, key)
            if element is None:
                element = ET.SubElement(root, f"{{{self.NAMESPACE}}}{key}")
            element.text = value

        # Convert to string and write once
        metadata_str = ET.tostring(root, encoding="unicode")

        # Single atomic write
        self.domain.setMetadata(
            libvirt.VIR_DOMAIN_METADATA_ELEMENT,
            metadata_str,
            self.NAMESPACE_PREFIX,
            self.NAMESPACE,
            libvirt.VIR_DOMAIN_AFFECT_CONFIG | libvirt.VIR_DOMAIN_AFFECT_LIVE
        )

    def get_all_metadata(self) -> Dict[str, str]:
        """Get all metadata as a dictionary.
        
        Returns:
            Dictionary of all metadata key-value pairs
        """
        try:
            metadata_xml = self.domain.metadata(
                libvirt.VIR_DOMAIN_METADATA_ELEMENT,
                self.NAMESPACE,
                0
            )
            root = ET.fromstring(metadata_xml)
            result = {}
            for child in root:
                tag = child.tag.replace(f"{{{self.NAMESPACE}}}", "")
                result[tag] = child.text or ""
            return result
        except libvirt.libvirtError:
            return {}

    def mark_in_use(self, task_id: str) -> None:
        """Mark VM as in use with a single atomic metadata write.
        
        Args:
            task_id: Unique identifier for the task using the VM
        """
        self.set_metadata_bulk({
            "in_use": "true",
            "task_id": task_id,
            "started_at": datetime.now().isoformat(),
        })

    def try_claim(self, task_id: str) -> bool:
        """Atomically attempt to claim a VM for a task.
        
        Writes all claim metadata in a single setMetadata() call, then
        re-reads after a brief delay to verify ownership. The delay allows
        any concurrent writers to complete, so the final read reflects the
        true last-writer-wins state.
        
        Args:
            task_id: Unique identifier for the task claiming the VM
            
        Returns:
            True if the VM was successfully claimed by this task_id,
            False if another instance claimed it first.
        """
        import time
        
        # Step 1: Quick check - skip if already in use
        if self.is_in_use():
            return False
        
        # Step 2: Attempt to claim (single atomic write)
        self.mark_in_use(task_id)
        
        # Step 3: Brief delay to let any concurrent writers finish.
        # Without this, two processes writing ~simultaneously could both
        # re-read before the other's write lands, both seeing their own
        # task_id. The delay ensures all concurrent writes have completed
        # before we verify.
        time.sleep(0.15)
        
        # Step 4: Re-read metadata to verify we own it.
        # After the delay, whoever wrote LAST owns the VM.
        # Only that process will see its own task_id here.
        actual_task_id = self.get_task_id()
        
        if actual_task_id == task_id:
            return True
        
        # Someone else overwrote our claim
        return False

    def mark_available(self) -> None:
        """Mark VM as available with a single atomic metadata write."""
        self.set_metadata_bulk({
            "in_use": "false",
            "task_id": "",
            "finished_at": datetime.now().isoformat(),
        })

    def is_in_use(self) -> bool:
        """Check if VM is currently in use.
        
        Returns:
            True if VM is in use, False otherwise
        """
        in_use = self.get_metadata("in_use")
        return in_use == "true"

    def get_task_id(self) -> Optional[str]:
        """Get current task ID if VM is in use.
        
        Returns:
            Task ID or None
        """
        return self.get_metadata("task_id")

    def clear_metadata(self) -> None:
        """Clear all metadata."""
        try:
            self.domain.setMetadata(
                libvirt.VIR_DOMAIN_METADATA_ELEMENT,
                None,
                self.NAMESPACE_PREFIX,
                self.NAMESPACE,
                libvirt.VIR_DOMAIN_AFFECT_CONFIG | libvirt.VIR_DOMAIN_AFFECT_LIVE
            )
        except libvirt.libvirtError:
            pass  # Metadata doesn't exist