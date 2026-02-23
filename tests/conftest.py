"""
Shared fixtures for all test modules.
"""
import pytest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, MagicMock
import libvirt


SAMPLE_DOMAIN_XML = """<domain type='kvm'>
  <name>{name}</name>
  <uuid>{uuid}</uuid>
  <description>{description}</description>
  <memory unit='KiB'>4194304</memory>
  <vcpu placement='static'>4</vcpu>
  <os>
    <type arch='x86_64'>hvm</type>
  </os>
  <devices>
    <interface type='network'>
      <source network='mgmt-network'/>
      <target dev='vnet100'/>
    </interface>
  </devices>
</domain>"""


SAMPLE_METADATA_XML = """<avm:metadata xmlns:avm="http://ansible-vm-manager.local/metadata">
  <avm:in_use>{in_use}</avm:in_use>
  <avm:task_id>{task_id}</avm:task_id>
  <avm:started_at>{started_at}</avm:started_at>
</avm:metadata>"""


def make_mock_domain(
    name="test-vm",
    uuid="test-uuid-123",
    tags=None,
    state=libvirt.VIR_DOMAIN_RUNNING,
    in_use=False,
    task_id="",
    started_at=""
):
    """Create a mock libvirt domain with configurable attributes.
    
    Args:
        name: VM name
        uuid: VM UUID
        tags: List of tags (default: ["linux-test"])
        state: Domain state constant
        in_use: Whether metadata shows in_use
        task_id: Metadata task_id value
        started_at: Metadata started_at value
    """
    if tags is None:
        tags = ["linux-test"]

    domain = Mock(spec=libvirt.virDomain)
    domain.name.return_value = name
    domain.UUIDString.return_value = uuid
    domain.state.return_value = [state, 0]
    domain.isActive.return_value = (state == libvirt.VIR_DOMAIN_RUNNING)

    # Build description from tags
    description = "tags: " + ", ".join(tags) if tags else ""

    xml = SAMPLE_DOMAIN_XML.format(
        name=name,
        uuid=uuid,
        description=description,
    )
    domain.XMLDesc.return_value = xml

    # Metadata handling — supports both DESCRIPTION and ELEMENT types.
    # VIR_DOMAIN_METADATA_DESCRIPTION (0): returns the tags description string
    # VIR_DOMAIN_METADATA_ELEMENT (2): returns the avm custom metadata XML
    has_element_metadata = bool(in_use or task_id or started_at)
    element_metadata_xml = None
    if has_element_metadata:
        element_metadata_xml = SAMPLE_METADATA_XML.format(
            in_use="true" if in_use else "false",
            task_id=task_id,
            started_at=started_at,
        )

    def _metadata_side_effect(type_val, uri, flags=0):
        if type_val == libvirt.VIR_DOMAIN_METADATA_DESCRIPTION:
            if description:
                return description
            raise libvirt.libvirtError("No domain metadata")
        if type_val == libvirt.VIR_DOMAIN_METADATA_ELEMENT:
            if element_metadata_xml:
                return element_metadata_xml
            raise libvirt.libvirtError("No metadata")
        raise libvirt.libvirtError("No metadata")

    domain.metadata.side_effect = _metadata_side_effect
    domain.setMetadata.return_value = None
    domain.create.return_value = 0

    return domain


def make_mock_conn(domains=None):
    """Create a mock libvirt connection with optional domain list.
    
    Args:
        domains: List of mock domains to return from listAllDomains
    """
    conn = Mock(spec=libvirt.virConnect)
    if domains is not None:
        conn.listAllDomains.return_value = domains
    else:
        conn.listAllDomains.return_value = []
    conn.defineXML.return_value = Mock()
    conn.domainEventRegisterAny.return_value = 42
    return conn


@pytest.fixture
def mock_domain():
    """Create a basic mock domain."""
    return make_mock_domain()


@pytest.fixture
def mock_conn():
    """Create a basic mock libvirt connection."""
    return make_mock_conn()
