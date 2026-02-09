"""
Unit tests for vm_tools_common.vm_operations — VM tag management, IP resolution,
and state string conversion.
"""
import pytest
import libvirt
from unittest.mock import Mock, patch, call

from vm_tools_common.vm_operations import (
    get_vm_tags,
    add_vm_tag,
    remove_vm_tag,
    get_vm_ip,
    get_state_string,
)
from tests.conftest import make_mock_domain, make_mock_conn, SAMPLE_DOMAIN_XML


# ---------------------------------------------------------------------------
# Helper: build domain XML with a custom <description> (or without one)
# ---------------------------------------------------------------------------

def _domain_xml(description=None, name="test-vm", network="mgmt-network", target_dev="vnet100"):
    """Build minimal domain XML with optional description element."""
    desc_block = ""
    if description is not None:
        desc_block = f"  <description>{description}</description>\n"

    return (
        f"<domain type='kvm'>\n"
        f"  <name>{name}</name>\n"
        f"  <uuid>test-uuid-123</uuid>\n"
        f"{desc_block}"
        f"  <memory unit='KiB'>4194304</memory>\n"
        f"  <vcpu placement='static'>4</vcpu>\n"
        f"  <os>\n"
        f"    <type arch='x86_64'>hvm</type>\n"
        f"  </os>\n"
        f"  <devices>\n"
        f"    <interface type='network'>\n"
        f"      <source network='{network}'/>\n"
        f"      <target dev='{target_dev}'/>\n"
        f"    </interface>\n"
        f"  </devices>\n"
        f"</domain>"
    )


def _make_domain(xml):
    """Create a Mock domain whose XMLDesc() returns *xml* for any flags."""
    domain = Mock(spec=libvirt.virDomain)
    domain.XMLDesc.return_value = xml
    return domain


# ===========================================================================
# get_vm_tags
# ===========================================================================

class TestGetVmTags:
    """Parsing tags from the <description> element of domain XML."""

    def test_single_tag(self):
        domain = _make_domain(_domain_xml("tags: linux-test"))
        assert get_vm_tags(domain) == ["linux-test"]

    def test_multiple_tags(self):
        domain = _make_domain(_domain_xml("tags: linux-test, linux-test-v1, used"))
        assert get_vm_tags(domain) == ["linux-test", "linux-test-v1", "used"]

    def test_no_tags_line(self):
        """Description exists but contains no 'tags:' prefix."""
        domain = _make_domain(_domain_xml("This VM is for integration tests"))
        assert get_vm_tags(domain) == []

    def test_no_description_element(self):
        """XML has no <description> at all."""
        domain = _make_domain(_domain_xml(description=None))
        assert get_vm_tags(domain) == []

    def test_empty_description(self):
        """<description></description> with empty text."""
        domain = _make_domain(_domain_xml(""))
        assert get_vm_tags(domain) == []

    def test_tags_with_extra_whitespace(self):
        """Tags with irregular spacing should still be parsed correctly."""
        domain = _make_domain(_domain_xml("tags:  alpha ,  beta  ,gamma"))
        assert get_vm_tags(domain) == ["alpha", "beta", "gamma"]

    def test_tags_line_after_other_text(self):
        """Multiline description where tags appear on a later line."""
        desc = "Some notes about this VM\ntags: web-server, production"
        domain = _make_domain(_domain_xml(desc))
        assert get_vm_tags(domain) == ["web-server", "production"]

    def test_tags_empty_after_colon(self):
        """'tags: ' with nothing after it should yield empty list."""
        domain = _make_domain(_domain_xml("tags: "))
        assert get_vm_tags(domain) == []

    def test_uses_inactive_xml_flag(self):
        """get_vm_tags must request XML with VIR_DOMAIN_XML_INACTIVE."""
        domain = _make_domain(_domain_xml("tags: a"))
        get_vm_tags(domain)
        domain.XMLDesc.assert_called_once_with(libvirt.VIR_DOMAIN_XML_INACTIVE)

    def test_uses_conftest_helper(self):
        """Verify compatibility with make_mock_domain from conftest."""
        domain = make_mock_domain(tags=["linux-test", "linux-test-v1"])
        assert get_vm_tags(domain) == ["linux-test", "linux-test-v1"]

    def test_conftest_domain_no_tags(self):
        domain = make_mock_domain(tags=[])
        assert get_vm_tags(domain) == []


# ===========================================================================
# add_vm_tag
# ===========================================================================

class TestAddVmTag:
    """Adding tags via description XML update."""

    def test_add_new_tag(self):
        """Adding a tag not yet present should update XML and call defineXML."""
        domain = _make_domain(_domain_xml("tags: existing"))
        conn = make_mock_conn()

        add_vm_tag(conn, domain, "new-tag")

        conn.defineXML.assert_called_once()
        new_xml = conn.defineXML.call_args[0][0]
        assert "existing" in new_xml
        assert "new-tag" in new_xml

    def test_add_duplicate_tag_is_idempotent(self):
        """Adding an already-present tag should not call defineXML."""
        domain = _make_domain(_domain_xml("tags: already-here"))
        conn = make_mock_conn()

        add_vm_tag(conn, domain, "already-here")

        conn.defineXML.assert_not_called()

    def test_add_tag_to_vm_with_no_existing_tags(self):
        """VM with no description element should get one created."""
        domain = _make_domain(_domain_xml(description=None))
        conn = make_mock_conn()

        add_vm_tag(conn, domain, "first-tag")

        conn.defineXML.assert_called_once()
        new_xml = conn.defineXML.call_args[0][0]
        assert "tags: first-tag" in new_xml

    def test_add_tag_to_empty_description(self):
        """VM with empty description should get tags line."""
        domain = _make_domain(_domain_xml(""))
        conn = make_mock_conn()

        add_vm_tag(conn, domain, "brand-new")

        conn.defineXML.assert_called_once()
        new_xml = conn.defineXML.call_args[0][0]
        assert "tags: brand-new" in new_xml

    def test_add_preserves_existing_tags(self):
        """All previous tags should be kept when a new one is appended."""
        domain = _make_domain(_domain_xml("tags: a, b"))
        conn = make_mock_conn()

        add_vm_tag(conn, domain, "c")

        new_xml = conn.defineXML.call_args[0][0]
        assert "tags: a, b, c" in new_xml

    def test_add_calls_xmldesc_with_inactive_flag(self):
        """add_vm_tag should read persistent (inactive) XML."""
        domain = _make_domain(_domain_xml("tags: x"))
        conn = make_mock_conn()

        add_vm_tag(conn, domain, "y")

        # XMLDesc is called twice: once in get_vm_tags, once in add_vm_tag body
        for c in domain.XMLDesc.call_args_list:
            assert c == call(libvirt.VIR_DOMAIN_XML_INACTIVE)


# ===========================================================================
# remove_vm_tag
# ===========================================================================

class TestRemoveVmTag:
    """Removing tags via description XML update."""

    def test_remove_existing_tag(self):
        """Removing a present tag should update XML without that tag."""
        domain = _make_domain(_domain_xml("tags: keep, remove-me"))
        conn = make_mock_conn()

        remove_vm_tag(conn, domain, "remove-me")

        conn.defineXML.assert_called_once()
        new_xml = conn.defineXML.call_args[0][0]
        assert "keep" in new_xml
        assert "remove-me" not in new_xml

    def test_remove_nonexistent_tag_is_noop(self):
        """Removing a tag that isn't present should not call defineXML."""
        domain = _make_domain(_domain_xml("tags: alpha"))
        conn = make_mock_conn()

        remove_vm_tag(conn, domain, "not-here")

        conn.defineXML.assert_not_called()

    def test_remove_last_tag_leaves_empty_tags_line(self):
        """Removing the only tag should leave 'tags: ' (empty)."""
        domain = _make_domain(_domain_xml("tags: lonely"))
        conn = make_mock_conn()

        remove_vm_tag(conn, domain, "lonely")

        conn.defineXML.assert_called_once()
        new_xml = conn.defineXML.call_args[0][0]
        assert "tags: " in new_xml
        assert "lonely" not in new_xml

    def test_remove_preserves_remaining_tags(self):
        """Only the specified tag is removed; others stay."""
        domain = _make_domain(_domain_xml("tags: a, b, c"))
        conn = make_mock_conn()

        remove_vm_tag(conn, domain, "b")

        new_xml = conn.defineXML.call_args[0][0]
        assert "tags: a, c" in new_xml

    def test_remove_from_vm_with_no_description(self):
        """Removing a tag from a VM with no description should be a no-op."""
        domain = _make_domain(_domain_xml(description=None))
        conn = make_mock_conn()

        remove_vm_tag(conn, domain, "anything")

        conn.defineXML.assert_not_called()

    def test_remove_calls_xmldesc_with_inactive_flag(self):
        domain = _make_domain(_domain_xml("tags: x"))
        conn = make_mock_conn()

        remove_vm_tag(conn, domain, "x")

        for c in domain.XMLDesc.call_args_list:
            assert c == call(libvirt.VIR_DOMAIN_XML_INACTIVE)


# ===========================================================================
# get_vm_ip
# ===========================================================================

class TestGetVmIp:
    """IP resolution via ARP, guest agent, and DHCP sources."""

    def _make_iface_result(self, iface_name, ipv4_addr):
        """Helper: build interfaceAddresses-style return dict."""
        return {
            iface_name: {
                "addrs": [
                    {"type": libvirt.VIR_IP_ADDR_TYPE_IPV4, "addr": ipv4_addr, "prefix": 24},
                ],
                "hwaddr": "52:54:00:aa:bb:cc",
            }
        }

    # --- ARP source ---

    def test_ip_from_arp(self):
        """First source (ARP) returns IP — should use it immediately."""
        domain = _make_domain(_domain_xml())
        domain.interfaceAddresses.return_value = self._make_iface_result("vnet100", "192.168.1.10")

        ip = get_vm_ip(domain)

        assert ip == "192.168.1.10"
        domain.interfaceAddresses.assert_called_once_with(
            libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP
        )

    # --- Fallback to agent ---

    def test_fallback_to_agent_when_arp_fails(self):
        """ARP raises libvirtError → should try guest agent next."""
        domain = _make_domain(_domain_xml())
        domain.interfaceAddresses.side_effect = [
            libvirt.libvirtError("ARP not available"),                       # ARP fails
            self._make_iface_result("eth0", "10.0.0.5"),                    # Agent succeeds
        ]

        ip = get_vm_ip(domain)

        assert ip == "10.0.0.5"
        assert domain.interfaceAddresses.call_count == 2

    def test_fallback_to_lease_when_arp_and_agent_fail(self):
        """ARP + agent fail → DHCP lease should be tried."""
        domain = _make_domain(_domain_xml())
        domain.interfaceAddresses.side_effect = [
            libvirt.libvirtError("ARP fail"),
            libvirt.libvirtError("Agent fail"),
            self._make_iface_result("vnet100", "172.16.0.3"),
        ]

        ip = get_vm_ip(domain)

        assert ip == "172.16.0.3"
        assert domain.interfaceAddresses.call_count == 3

    # --- No IP found ---

    def test_no_ip_found_all_sources_fail(self):
        """All sources raise errors — should return None."""
        domain = _make_domain(_domain_xml())
        domain.interfaceAddresses.side_effect = libvirt.libvirtError("no info")

        ip = get_vm_ip(domain)

        assert ip is None

    def test_no_ip_found_empty_addrs(self):
        """All sources return interfaces but with no addresses."""
        empty_ifaces = {"vnet100": {"addrs": [], "hwaddr": "52:54:00:aa:bb:cc"}}
        domain = _make_domain(_domain_xml())
        domain.interfaceAddresses.return_value = empty_ifaces

        ip = get_vm_ip(domain)

        assert ip is None

    # --- Network filtering ---

    def test_specific_network_returns_matching_ip(self):
        """When network is specified, return IP from corresponding interface."""
        domain = _make_domain(_domain_xml(network="my-network", target_dev="vnet200"))
        domain.interfaceAddresses.return_value = {
            "vnet200": {
                "addrs": [
                    {"type": libvirt.VIR_IP_ADDR_TYPE_IPV4, "addr": "192.168.50.1", "prefix": 24},
                ],
                "hwaddr": "52:54:00:11:22:33",
            },
            "vnet201": {
                "addrs": [
                    {"type": libvirt.VIR_IP_ADDR_TYPE_IPV4, "addr": "10.0.0.99", "prefix": 24},
                ],
                "hwaddr": "52:54:00:44:55:66",
            },
        }

        ip = get_vm_ip(domain, network="my-network")

        assert ip == "192.168.50.1"

    def test_specific_network_not_found_in_xml(self):
        """If the requested network doesn't exist in VM XML, return None."""
        domain = _make_domain(_domain_xml(network="other-network", target_dev="vnet300"))

        ip = get_vm_ip(domain, network="nonexistent-network")

        assert ip is None
        # interfaceAddresses should never be called
        domain.interfaceAddresses.assert_not_called()

    def test_specific_network_interface_has_no_addrs(self):
        """Network exists but interface has no addresses → return None."""
        domain = _make_domain(_domain_xml(network="my-net", target_dev="vnet400"))
        domain.interfaceAddresses.return_value = {
            "vnet400": {"addrs": [], "hwaddr": "52:54:00:00:00:01"},
        }

        ip = get_vm_ip(domain, network="my-net")

        assert ip is None

    # --- Loopback / IPv6 ---

    def test_loopback_ip_returned_when_only_option(self):
        """If 127.0.0.1 is the only IPv4 address, it is returned.

        The function does not filter out loopback — callers must handle this.
        """
        domain = _make_domain(_domain_xml())
        domain.interfaceAddresses.return_value = {
            "lo": {
                "addrs": [
                    {"type": libvirt.VIR_IP_ADDR_TYPE_IPV4, "addr": "127.0.0.1", "prefix": 8},
                ],
                "hwaddr": "00:00:00:00:00:00",
            }
        }

        ip = get_vm_ip(domain)

        assert ip == "127.0.0.1"

    def test_ipv6_only_interface_is_skipped(self):
        """Interfaces with only IPv6 addresses should not be returned."""
        IPV6_TYPE = 1  # VIR_IP_ADDR_TYPE_IPV6
        domain = _make_domain(_domain_xml())
        domain.interfaceAddresses.return_value = {
            "eth0": {
                "addrs": [
                    {"type": IPV6_TYPE, "addr": "fe80::1", "prefix": 64},
                ],
                "hwaddr": "52:54:00:aa:bb:cc",
            }
        }

        ip = get_vm_ip(domain)

        assert ip is None

    def test_multiple_interfaces_returns_first_ipv4(self):
        """With multiple interfaces, the first IPv4 address encountered is returned."""
        domain = _make_domain(_domain_xml())
        # Python 3.7+ dicts are ordered by insertion
        domain.interfaceAddresses.return_value = {
            "vnet100": {
                "addrs": [
                    {"type": libvirt.VIR_IP_ADDR_TYPE_IPV4, "addr": "192.168.1.1", "prefix": 24},
                ],
                "hwaddr": "52:54:00:aa:bb:01",
            },
            "vnet101": {
                "addrs": [
                    {"type": libvirt.VIR_IP_ADDR_TYPE_IPV4, "addr": "10.0.0.1", "prefix": 24},
                ],
                "hwaddr": "52:54:00:aa:bb:02",
            },
        }

        ip = get_vm_ip(domain)

        assert ip == "192.168.1.1"

    # --- Source ordering ---

    def test_sources_tried_in_order(self):
        """ARP → Agent → Lease is the expected call order."""
        domain = _make_domain(_domain_xml())
        domain.interfaceAddresses.side_effect = [
            libvirt.libvirtError("ARP fail"),
            libvirt.libvirtError("Agent fail"),
            libvirt.libvirtError("Lease fail"),
        ]

        get_vm_ip(domain)

        expected_calls = [
            call(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_ARP),
            call(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_AGENT),
            call(libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE),
        ]
        assert domain.interfaceAddresses.call_args_list == expected_calls


# ===========================================================================
# get_state_string
# ===========================================================================

class TestGetStateString:
    """Converting libvirt state constants to human-readable strings."""

    @pytest.mark.parametrize(
        "state_const, expected",
        [
            (libvirt.VIR_DOMAIN_RUNNING, "running"),
            (libvirt.VIR_DOMAIN_SHUTOFF, "shutoff"),
            (libvirt.VIR_DOMAIN_PAUSED, "paused"),
            (libvirt.VIR_DOMAIN_SHUTDOWN, "shutdown"),
            (libvirt.VIR_DOMAIN_CRASHED, "crashed"),
            (libvirt.VIR_DOMAIN_BLOCKED, "blocked"),
            (libvirt.VIR_DOMAIN_NOSTATE, "no state"),
            (libvirt.VIR_DOMAIN_PMSUSPENDED, "suspended"),
        ],
    )
    def test_known_states(self, state_const, expected):
        assert get_state_string(state_const) == expected

    def test_unknown_state_returns_unknown(self):
        """An unrecognised integer should map to 'unknown'."""
        assert get_state_string(9999) == "unknown"

    def test_negative_state_returns_unknown(self):
        assert get_state_string(-1) == "unknown"
