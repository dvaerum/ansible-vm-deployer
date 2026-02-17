"""
VM operations for libvirt domains.

Provides functions for tag management, IP resolution, and network interface queries.
"""
import logging
import libvirt
import xml.etree.ElementTree as ET
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)


def get_vm_tags(domain: libvirt.virDomain) -> List[str]:
    """Get tags for a VM from its XML description.
    
    Args:
        domain: libvirt domain object
        
    Returns:
        List of tags
    """
    # Use inactive XML to get persistent config (where tags are stored)
    xml_desc = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    root = ET.fromstring(xml_desc)
    
    # Look for metadata tags in description or custom metadata
    tags = []
    
    # Check description field
    desc_elem = root.find(".//description")
    if desc_elem is not None and desc_elem.text:
        # Parse tags from description (format: tags: tag1, tag2, tag3)
        for line in desc_elem.text.split("\n"):
            if line.strip().startswith("tags:"):
                tag_str = line.split("tags:", 1)[1]
                tags.extend([t.strip() for t in tag_str.split(",") if t.strip()])
    
    return tags


def add_vm_tag(conn: libvirt.virConnect, domain: libvirt.virDomain, tag: str) -> None:
    """Add a tag to VM's description.
    
    Args:
        conn: libvirt connection object
        domain: libvirt domain object
        tag: Tag to add
    """
    # Get current tags
    current_tags = get_vm_tags(domain)
    
    # Don't add if already present
    if tag in current_tags:
        return
    
    current_tags.append(tag)
    
    # Update description
    xml_desc = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    root = ET.fromstring(xml_desc)
    
    desc_elem = root.find(".//description")
    if desc_elem is None:
        desc_elem = ET.SubElement(root, "description")
    
    # Build new description with tags
    desc_elem.text = "tags: " + ", ".join(current_tags)
    
    # Update domain XML
    new_xml = ET.tostring(root, encoding="unicode")
    conn.defineXML(new_xml)


def remove_vm_tag(conn: libvirt.virConnect, domain: libvirt.virDomain, tag: str) -> None:
    """Remove a tag from VM's description.
    
    Args:
        conn: libvirt connection object
        domain: libvirt domain object
        tag: Tag to remove
    """
    # Get current tags
    current_tags = get_vm_tags(domain)
    
    # Remove the tag if present
    if tag not in current_tags:
        return
    
    current_tags.remove(tag)
    
    # Update description
    xml_desc = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
    root = ET.fromstring(xml_desc)
    
    desc_elem = root.find(".//description")
    if desc_elem is None:
        return
    
    # Build new description with remaining tags
    if current_tags:
        desc_elem.text = "tags: " + ", ".join(current_tags)
    else:
        desc_elem.text = "tags: "
    
    # Update domain XML
    new_xml = ET.tostring(root, encoding="unicode")
    conn.defineXML(new_xml)


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
