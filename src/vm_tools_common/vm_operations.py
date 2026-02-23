"""
VM operations for libvirt domains.

Provides functions for tag management, IP resolution, and network interface queries.
"""
import logging
import libvirt
import xml.etree.ElementTree as ET
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)


def _parse_tags_from_description(description: str) -> List[str]:
    """Parse tags from a description string.

    Args:
        description: The description text (may be multiline)

    Returns:
        List of tag strings
    """
    tags: List[str] = []
    for line in description.split("\n"):
        if line.strip().startswith("tags:"):
            tag_str = line.split("tags:", 1)[1]
            tags.extend([t.strip() for t in tag_str.split(",") if t.strip()])
    return tags


def _build_description(tags: List[str]) -> str:
    """Build a description string from a list of tags.

    Args:
        tags: List of tag strings

    Returns:
        Description string in 'tags: a, b, c' format
    """
    return "tags: " + ", ".join(tags)


def _get_description(domain: libvirt.virDomain) -> Optional[str]:
    """Read the persistent description from a domain.

    Tries the metadata API first (``domain.metadata()``), which returns the
    description directly without needing full XML parsing.  Falls back to
    XML parsing if the metadata API is unavailable or returns no metadata.

    Args:
        domain: libvirt domain object

    Returns:
        Description string, or None if no description is set
    """
    try:
        desc = domain.metadata(
            libvirt.VIR_DOMAIN_METADATA_DESCRIPTION,
            None,
            libvirt.VIR_DOMAIN_AFFECT_CONFIG,
        )
        if desc:
            return desc
    except libvirt.libvirtError:
        # VIR_ERR_NO_DOMAIN_METADATA or older libvirt — fall through to XML
        pass

    # Fallback: parse full inactive XML
    xml_desc = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    root = ET.fromstring(xml_desc)
    desc_elem = root.find(".//description")
    if desc_elem is not None and desc_elem.text:
        return desc_elem.text
    return None


def _set_description(domain: libvirt.virDomain, description: str) -> None:
    """Write the description to a domain, updating both live and persistent config.

    Uses ``domain.setMetadata()`` which correctly updates both the live
    (running) and persistent (inactive) configurations atomically.  The
    previous approach of reading inactive XML + ``defineXML()`` only updated
    the persistent config; on a running VM the live config could overwrite
    the persistent config in certain situations (e.g. on save/restore),
    causing tag changes to silently revert.

    For shutoff VMs only the persistent config is updated (``AFFECT_LIVE``
    is not valid when the domain is not running).

    Args:
        domain: libvirt domain object
        description: New description text
    """
    flags = libvirt.VIR_DOMAIN_AFFECT_CONFIG
    try:
        if domain.isActive():
            flags |= libvirt.VIR_DOMAIN_AFFECT_LIVE
    except libvirt.libvirtError:
        # If we can't determine state, just update the persistent config.
        # This is the safe fallback — it's what defineXML() used to do.
        pass

    domain.setMetadata(
        libvirt.VIR_DOMAIN_METADATA_DESCRIPTION,
        description,
        None,
        None,
        flags,
    )


def get_vm_tags(domain: libvirt.virDomain) -> List[str]:
    """Get tags for a VM from its description.

    Reads the persistent (inactive) domain description and parses the
    ``tags: tag1, tag2, ...`` line.

    Args:
        domain: libvirt domain object

    Returns:
        List of tags
    """
    description = _get_description(domain)
    if not description:
        return []
    return _parse_tags_from_description(description)


def add_vm_tag(conn: libvirt.virConnect, domain: libvirt.virDomain, tag: str) -> None:
    """Add a tag to VM's description.

    Uses ``domain.setMetadata()`` so the change is applied to both the live
    and persistent configurations of running VMs.  The ``conn`` parameter is
    retained for API compatibility but is no longer used internally.

    Args:
        conn: libvirt connection object (unused, kept for API compatibility)
        domain: libvirt domain object
        tag: Tag to add
    """
    current_tags = get_vm_tags(domain)

    if tag in current_tags:
        return

    current_tags.append(tag)
    _set_description(domain, _build_description(current_tags))


def remove_vm_tag(conn: libvirt.virConnect, domain: libvirt.virDomain, tag: str) -> None:
    """Remove a tag from VM's description.

    Uses ``domain.setMetadata()`` so the change is applied to both the live
    and persistent configurations of running VMs.  The ``conn`` parameter is
    retained for API compatibility but is no longer used internally.

    Args:
        conn: libvirt connection object (unused, kept for API compatibility)
        domain: libvirt domain object
        tag: Tag to remove
    """
    current_tags = get_vm_tags(domain)

    if tag not in current_tags:
        return

    current_tags.remove(tag)
    _set_description(domain, _build_description(current_tags))


def get_network_to_interface_mapping(domain: libvirt.virDomain) -> Dict[str, str]:
    """Get mapping of libvirt network names to interface names.
    
    Args:
        domain: libvirt domain object
        
    Returns:
        Dictionary mapping network names to interface names
        Example: {"mgmt-network": "vnet556", "data-network": "vnet557"}
    """
    xml_desc = domain.XMLDesc(0)
    root = ET.fromstring(xml_desc)
    
    mapping = {}
    # Find all network interfaces
    for interface in root.findall(".//interface[@type='network']"):
        source = interface.find("source")
        target = interface.find("target")
        
        if source is not None and target is not None:
            network_name = source.get("network")
            interface_name = target.get("dev")
            
            if network_name and interface_name:
                mapping[network_name] = interface_name
    
    return mapping


def get_vm_ip(domain: libvirt.virDomain, network: Optional[str] = None) -> Optional[str]:
    """Get IP address of a VM.
    
    Tries multiple sources in order:
    1. ARP table (most reliable)
    2. QEMU guest agent
    3. DHCP leases
    
    Args:
        domain: libvirt domain object
        network: Optional libvirt network name (e.g., 'mgmt-network')
                If specified, only return IP from this network.
                If None, return first IP found.
        
    Returns:
        IP address or None
    """
    # If network is specified, convert it to interface name
    target_interface = None
    if network:
        mapping = get_network_to_interface_mapping(domain)
        target_interface = mapping.get(network)
        if not target_interface:
            # Network name not found in VM's interfaces
            return None
    
    # Try different sources in order of reliability
    sources = [
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP,    # ARP table
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT,  # QEMU guest agent
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE,  # DHCP leases
    ]
    
    for source in sources:
        try:
            ifaces = domain.interfaceAddresses(source)
            
            # If specific interface requested, only check that one
            if target_interface:
                if target_interface in ifaces and ifaces[target_interface]["addrs"]:
                    for addr in ifaces[target_interface]["addrs"]:
                        if addr["type"] == libvirt.VIR_IP_ADDR_TYPE_IPV4:
                            ip = addr["addr"]
                            if ip.startswith("127."):
                                logger.debug(
                                    "Skipping loopback address %s for VM %s",
                                    ip, domain.name(),
                                )
                                continue
                            return ip
            else:
                # Return first IP found from any interface
                for iface_name, iface_info in ifaces.items():
                    if iface_info["addrs"]:
                        for addr in iface_info["addrs"]:
                            if addr["type"] == libvirt.VIR_IP_ADDR_TYPE_IPV4:
                                ip = addr["addr"]
                                if ip.startswith("127."):
                                    logger.debug(
                                        "Skipping loopback address %s for VM %s",
                                        ip, domain.name(),
                                    )
                                    continue
                                return ip
        except libvirt.libvirtError:
            continue
    
    return None


def list_vm_interfaces(domain: libvirt.virDomain) -> Dict[str, Dict[str, List[str]]]:
    """List all network interfaces and their IPs for a VM.
    
    Args:
        domain: libvirt domain object
        
    Returns:
        Dictionary with 'networks' and 'interfaces' keys:
        {
            "networks": {"mgmt-network": ["192.168.1.102"]},
            "interfaces": {"vnet556": ["192.168.1.102"]}
        }
    """
    interfaces_result: Dict[str, List[str]] = {}
    
    sources = [
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP,
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT,
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE,
    ]
    
    for source in sources:
        try:
            ifaces = domain.interfaceAddresses(source)
            for iface_name, iface_info in ifaces.items():
                if iface_name not in interfaces_result:
                    interfaces_result[iface_name] = []
                
                if iface_info["addrs"]:
                    for addr in iface_info["addrs"]:
                        if addr["type"] == libvirt.VIR_IP_ADDR_TYPE_IPV4:
                            ip = addr["addr"]
                            if ip not in interfaces_result[iface_name]:
                                interfaces_result[iface_name].append(ip)
        except libvirt.libvirtError:
            continue
    
    # Build network mapping
    network_mapping = get_network_to_interface_mapping(domain)
    # Reverse mapping: interface -> network
    interface_to_network = {v: k for k, v in network_mapping.items()}
    
    # Build networks result
    networks_result: Dict[str, List[str]] = {}
    for iface_name, ips in interfaces_result.items():
        if iface_name in interface_to_network:
            network_name = interface_to_network[iface_name]
            networks_result[network_name] = ips
    
    return {
        "networks": networks_result,
        "interfaces": interfaces_result
    }


def get_state_string(state: int) -> str:
    """Convert libvirt state constant to string.
    
    Args:
        state: libvirt state constant
        
    Returns:
        State string
    """
    states = {
        libvirt.VIR_DOMAIN_NOSTATE: "no state",
        libvirt.VIR_DOMAIN_RUNNING: "running",
        libvirt.VIR_DOMAIN_BLOCKED: "blocked",
        libvirt.VIR_DOMAIN_PAUSED: "paused",
        libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown",
        libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
        libvirt.VIR_DOMAIN_CRASHED: "crashed",
        libvirt.VIR_DOMAIN_PMSUSPENDED: "suspended",
    }
    return states.get(state, "unknown")
