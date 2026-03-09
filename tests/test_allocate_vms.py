"""
Comprehensive unit tests for VMManager.allocate_vms() and
VMManager.find_available_vms_by_tags().

These tests mock libvirt objects, MetadataManager, and get_vm_tags/vm_matches_tags
to validate VM selection, tag filtering, state checks, claim races, and ordering.
"""
import libvirt
import pytest
from unittest.mock import Mock, patch, call

from ansible_deployer.vm_manager import VMManager
from ansible_deployer.config import LibvirtConnectionConfig
from tests.conftest import make_mock_domain, make_mock_conn


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_domain(name, tags, state=libvirt.VIR_DOMAIN_RUNNING, in_use=False):
    """Shorthand wrapper around the shared conftest helper.

    Sets up the mock domain so that:
      - ``get_vm_tags`` (which reads XMLDesc) returns *tags*
      - ``MetadataManager.is_in_use()`` returns *in_use*
      - ``domain.state()`` returns *[state, 0]*
    """
    return make_mock_domain(name=name, tags=tags, state=state, in_use=in_use)


def _build_manager(domains):
    """Return a VMManager with a mock connection returning *domains*.

    Injects a mock connection directly into the internal dicts so that
    the manager thinks it is already connected (bypassing libvirt.open).
    """
    mgr = VMManager(uri="test:///default")
    mock_conn = make_mock_conn(domains)
    mgr._connections = {"default": mock_conn}
    mgr._connected_configs = {"default": LibvirtConnectionConfig(uri="test:///default")}
    return mgr


# ---------------------------------------------------------------------------
# Tests for allocate_vms()
# ---------------------------------------------------------------------------

class TestAllocateVms:
    """Tests for VMManager.allocate_vms()."""

    # 1. Basic allocation: 4 VMs available, request 1 -> gets first match
    def test_basic_allocation_picks_first_match(self):
        domains = [
            _make_domain(f"vm-{i}", ["linux-test"])
            for i in range(4)
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(["linux-test"], count=1, task_id="task-1")

        assert len(result) == 1
        assert result[0].name() == "vm-0"

    # 2. Exclude tags work: 15 VMs, 4 have "used" tag -> those 4 skipped
    def test_exclude_tags_skip_used_vms(self):
        domains = []
        for i in range(15):
            tags = ["linux-test", "used"] if i < 4 else ["linux-test"]
            domains.append(_make_domain(f"vm-{i}", tags))

        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(
                ["linux-test"], count=3, task_id="task-2",
                exclude_tags=["used"],
            )

        assert len(result) == 3
        # The first 4 VMs (vm-0..vm-3) have "used" and must be skipped.
        # The first match is vm-4.
        names = [d.name() for d in result]
        assert names == ["vm-4", "vm-5", "vm-6"]

    # 3. All VMs have "used" tag -> returns empty list
    def test_all_vms_have_used_tag_returns_empty(self):
        domains = [
            _make_domain(f"vm-{i}", ["linux-test", "used"])
            for i in range(5)
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(
                ["linux-test"], count=2, task_id="task-3",
                exclude_tags=["used"],
            )

        assert result == []

    # 4. Mixed states: some running, some shutoff -> only running selected
    def test_mixed_states_only_running_selected(self):
        d_running = _make_domain("vm-run", ["linux-test"],
                                 state=libvirt.VIR_DOMAIN_RUNNING)
        d_shutoff = _make_domain("vm-off", ["linux-test"],
                                 state=libvirt.VIR_DOMAIN_SHUTOFF)
        d_paused = _make_domain("vm-pause", ["linux-test"],
                                state=libvirt.VIR_DOMAIN_PAUSED)
        d_running2 = _make_domain("vm-run2", ["linux-test"],
                                  state=libvirt.VIR_DOMAIN_RUNNING)

        mgr = _build_manager([d_shutoff, d_paused, d_running, d_running2])

        # A shared mock instance is correct here: non-running VMs are
        # filtered by state before MetadataManager is ever constructed
        # (vm_manager.py:415-417), so only the two running VMs reach
        # the is_in_use / try_claim calls.
        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(
                ["linux-test"], count=4, task_id="task-4",
            )

        names = [d.name() for d in result]
        assert names == ["vm-run", "vm-run2"]

    # 5. Concurrent claim race: try_claim returns False for first VM
    def test_try_claim_failure_moves_to_next(self):
        domains = [
            _make_domain("vm-contested", ["linux-test"]),
            _make_domain("vm-available", ["linux-test"]),
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            # First try_claim fails (another process won), second succeeds
            instance.try_claim.side_effect = [False, True]

            result = mgr.allocate_vms(
                ["linux-test"], count=1, task_id="task-5",
            )

        assert len(result) == 1
        assert result[0].name() == "vm-available"

    # 6. Request more than available: request 4 but only 2 match
    def test_request_more_than_available(self):
        domains = [
            _make_domain("vm-a", ["linux-test"]),
            _make_domain("vm-b", ["other-tag"]),
            _make_domain("vm-c", ["linux-test"]),
            _make_domain("vm-d", ["other-tag"]),
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(
                ["linux-test"], count=4, task_id="task-6",
            )

        assert len(result) == 2
        names = [d.name() for d in result]
        assert names == ["vm-a", "vm-c"]

    # 7. Already in_use: VMs with is_in_use() = True -> skipped
    def test_in_use_vms_skipped(self):
        d_busy = _make_domain("vm-busy", ["linux-test"], in_use=True)
        d_free = _make_domain("vm-free", ["linux-test"], in_use=False)

        mgr = _build_manager([d_busy, d_free])

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            # Return different mock instances per domain to simulate
            # different is_in_use results per VM.
            mm_busy = Mock()
            mm_busy.is_in_use.return_value = True

            mm_free = Mock()
            mm_free.is_in_use.return_value = False
            mm_free.try_claim.return_value = True

            MockMM.side_effect = [mm_busy, mm_free]

            result = mgr.allocate_vms(
                ["linux-test"], count=2, task_id="task-7",
            )

        assert len(result) == 1
        assert result[0].name() == "vm-free"
        # try_claim should never be called for busy VM
        mm_busy.try_claim.assert_not_called()

    # 8. Broken + used tags: both in exclude_tags -> excluded
    def test_broken_and_used_tags_both_excluded(self):
        domains = [
            _make_domain("vm-used", ["linux-test", "used"]),
            _make_domain("vm-broken", ["linux-test", "broken"]),
            _make_domain("vm-both", ["linux-test", "used", "broken"]),
            _make_domain("vm-clean", ["linux-test"]),
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(
                ["linux-test"], count=4, task_id="task-8",
                exclude_tags=["used", "broken"],
            )

        assert len(result) == 1
        assert result[0].name() == "vm-clean"

    # 9. Selection order: picks first N matching VMs in listAllDomains order
    def test_selection_order_is_deterministic(self):
        domains = [
            _make_domain(f"vm-{i}", ["linux-test"])
            for i in range(10)
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(
                ["linux-test"], count=3, task_id="task-9",
            )

        names = [d.name() for d in result]
        assert names == ["vm-0", "vm-1", "vm-2"]

    # Edge: empty domain list -> returns empty
    def test_empty_domain_list(self):
        mgr = _build_manager([])

        with patch("ansible_deployer.vm_manager.MetadataManager"):
            result = mgr.allocate_vms(
                ["linux-test"], count=1, task_id="task-empty",
            )

        assert result == []

    # Edge: count=0 -> returns empty immediately
    def test_count_zero_returns_empty(self):
        domains = [_make_domain("vm-0", ["linux-test"])]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager"):
            result = mgr.allocate_vms(
                ["linux-test"], count=0, task_id="task-zero",
            )

        assert result == []

    # Edge: not connected -> raises RuntimeError
    def test_not_connected_raises(self):
        mgr = VMManager(uri="test:///default")

        with pytest.raises(RuntimeError, match="Not connected"):
            mgr.allocate_vms(["linux-test"], count=1, task_id="x")

    # Verify try_claim receives the correct task_id
    def test_try_claim_receives_task_id(self):
        domains = [_make_domain("vm-0", ["linux-test"])]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            mgr.allocate_vms(
                ["linux-test"], count=1, task_id="unique-task-42",
            )

            instance.try_claim.assert_called_once_with("unique-task-42")

    # All try_claim fail -> returns empty list
    def test_all_claims_fail_returns_empty(self):
        domains = [
            _make_domain(f"vm-{i}", ["linux-test"])
            for i in range(3)
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = False  # every claim loses

            result = mgr.allocate_vms(
                ["linux-test"], count=2, task_id="task-loser",
            )

        assert result == []
        assert instance.try_claim.call_count == 3

    # exclude_tags defaults to empty when None is passed
    def test_exclude_tags_none_defaults_to_empty(self):
        domains = [_make_domain("vm-0", ["linux-test"])]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(
                ["linux-test"], count=1, task_id="task-none",
                exclude_tags=None,
            )

        assert len(result) == 1

    # Stops iterating once count is reached
    def test_stops_at_count(self):
        domains = [
            _make_domain(f"vm-{i}", ["linux-test"])
            for i in range(10)
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(
                ["linux-test"], count=2, task_id="task-stop",
            )

        assert len(result) == 2
        # try_claim should have been called exactly 2 times (stopped early)
        assert instance.try_claim.call_count == 2


# ---------------------------------------------------------------------------
# Tests for find_available_vms_by_tags()  (no claiming)
# ---------------------------------------------------------------------------

class TestFindAvailableVmsByTags:
    """Tests for VMManager.find_available_vms_by_tags()."""

    def test_basic_find(self):
        domains = [
            _make_domain(f"vm-{i}", ["linux-test"])
            for i in range(5)
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vms_by_tags(
                ["linux-test"], count=2,
            )

        assert len(result) == 2
        assert [d.name() for d in result] == ["vm-0", "vm-1"]

    def test_find_excludes_used_tag(self):
        domains = [
            _make_domain("vm-used", ["linux-test", "used"]),
            _make_domain("vm-clean", ["linux-test"]),
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vms_by_tags(
                ["linux-test"], count=2, exclude_tags=["used"],
            )

        assert len(result) == 1
        assert result[0].name() == "vm-clean"

    def test_find_skips_shutoff(self):
        domains = [
            _make_domain("vm-off", ["linux-test"],
                         state=libvirt.VIR_DOMAIN_SHUTOFF),
            _make_domain("vm-run", ["linux-test"]),
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vms_by_tags(
                ["linux-test"], count=2,
            )

        assert len(result) == 1
        assert result[0].name() == "vm-run"

    def test_find_skips_in_use(self):
        domains = [
            _make_domain("vm-busy", ["linux-test"], in_use=True),
            _make_domain("vm-free", ["linux-test"]),
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            mm_busy = Mock()
            mm_busy.is_in_use.return_value = True

            mm_free = Mock()
            mm_free.is_in_use.return_value = False

            MockMM.side_effect = [mm_busy, mm_free]

            result = mgr.find_available_vms_by_tags(
                ["linux-test"], count=2,
            )

        assert len(result) == 1
        assert result[0].name() == "vm-free"

    def test_find_returns_fewer_than_count(self):
        domains = [_make_domain("vm-only", ["linux-test"])]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vms_by_tags(
                ["linux-test"], count=5,
            )

        assert len(result) == 1

    def test_find_preserves_order(self):
        domains = [
            _make_domain(f"vm-{i}", ["linux-test"])
            for i in range(6)
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vms_by_tags(
                ["linux-test"], count=4,
            )

        names = [d.name() for d in result]
        assert names == ["vm-0", "vm-1", "vm-2", "vm-3"]

    def test_find_empty_domain_list(self):
        mgr = _build_manager([])

        with patch("ansible_deployer.vm_manager.MetadataManager"):
            result = mgr.find_available_vms_by_tags(
                ["linux-test"], count=1,
            )

        assert result == []

    def test_find_not_connected_raises(self):
        mgr = VMManager(uri="test:///default")

        with pytest.raises(RuntimeError, match="Not connected"):
            mgr.find_available_vms_by_tags(["linux-test"], count=1)

    def test_find_does_not_call_try_claim(self):
        """find_available_vms_by_tags must never attempt to claim VMs."""
        domains = [_make_domain("vm-0", ["linux-test"])]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            mgr.find_available_vms_by_tags(["linux-test"], count=1)

            instance.try_claim.assert_not_called()

    def test_find_stops_at_count(self):
        """Should stop iterating once *count* VMs are collected."""
        domains = [
            _make_domain(f"vm-{i}", ["linux-test"])
            for i in range(10)
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vms_by_tags(
                ["linux-test"], count=3,
            )

        assert len(result) == 3
        # is_in_use was checked only for the first 3 (loop breaks at count)
        assert instance.is_in_use.call_count == 3

    def test_find_exclude_tags_none_defaults(self):
        domains = [_make_domain("vm-0", ["linux-test"])]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vms_by_tags(
                ["linux-test"], count=1, exclude_tags=None,
            )

        assert len(result) == 1


# ---------------------------------------------------------------------------
# Tests for find_available_vm_by_tags()  (single VM variant)
# ---------------------------------------------------------------------------

class TestFindAvailableVmByTags:
    """Tests for VMManager.find_available_vm_by_tags() (singular)."""

    def test_returns_first_match(self):
        domains = [
            _make_domain("vm-0", ["linux-test"]),
            _make_domain("vm-1", ["linux-test"]),
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vm_by_tags(["linux-test"])

        assert result is not None
        assert result.name() == "vm-0"

    def test_returns_none_when_no_match(self):
        domains = [
            _make_domain("vm-0", ["other-tag"]),
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vm_by_tags(["linux-test"])

        assert result is None

    def test_skips_used_tag(self):
        domains = [
            _make_domain("vm-used", ["linux-test", "used"]),
            _make_domain("vm-clean", ["linux-test"]),
        ]
        mgr = _build_manager(domains)

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vm_by_tags(
                ["linux-test"], exclude_tags=["used"],
            )

        assert result is not None
        assert result.name() == "vm-clean"

    def test_not_connected_raises(self):
        mgr = VMManager(uri="test:///default")

        with pytest.raises(RuntimeError, match="Not connected"):
            mgr.find_available_vm_by_tags(["linux-test"])


# ---------------------------------------------------------------------------
# Tests for auto-exclude of 'broken' tag
# ---------------------------------------------------------------------------

class TestAutoExcludeBrokenTag:
    """Verify that 'broken' is always excluded from allocation, even if
    the caller doesn't explicitly pass it in exclude_tags."""

    def test_allocate_vms_auto_excludes_broken(self):
        """allocate_vms auto-excludes 'broken' when exclude_tags is None."""
        broken_vm = _make_domain("broken-vm", ["linux-test", "broken"])
        clean_vm = _make_domain("clean-vm", ["linux-test"])
        mgr = _build_manager([broken_vm, clean_vm])

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            # No exclude_tags passed — broken should still be excluded
            result = mgr.allocate_vms(["linux-test"], count=2, task_id="t1")

        names = [d.name() for d in result]
        assert "broken-vm" not in names
        assert "clean-vm" in names

    def test_allocate_vms_auto_excludes_broken_with_existing_excludes(self):
        """allocate_vms auto-excludes 'broken' alongside user-provided excludes."""
        broken_vm = _make_domain("broken-vm", ["linux-test", "broken"])
        used_vm = _make_domain("used-vm", ["linux-test", "used"])
        clean_vm = _make_domain("clean-vm", ["linux-test"])
        mgr = _build_manager([broken_vm, used_vm, clean_vm])

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(
                ["linux-test"], count=3, task_id="t1",
                exclude_tags=["used"]
            )

        names = [d.name() for d in result]
        assert "broken-vm" not in names
        assert "used-vm" not in names
        assert "clean-vm" in names

    def test_allocate_vms_no_duplicate_broken_exclusion(self):
        """If 'broken' is already in exclude_tags, don't add it twice."""
        clean_vm = _make_domain("clean-vm", ["linux-test"])
        mgr = _build_manager([clean_vm])

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            # Explicitly pass broken — should not cause issues
            result = mgr.allocate_vms(
                ["linux-test"], count=1, task_id="t1",
                exclude_tags=["broken"]
            )

        assert len(result) == 1

    def test_find_available_vm_by_tags_auto_excludes_broken(self):
        """find_available_vm_by_tags auto-excludes 'broken'."""
        broken_vm = _make_domain("broken-vm", ["linux-test", "broken"])
        clean_vm = _make_domain("clean-vm", ["linux-test"])
        mgr = _build_manager([broken_vm, clean_vm])

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vm_by_tags(["linux-test"])

        assert result is not None
        assert result.name() == "clean-vm"

    def test_find_available_vms_by_tags_auto_excludes_broken(self):
        """find_available_vms_by_tags auto-excludes 'broken'."""
        broken_vm = _make_domain("broken-vm", ["linux-test", "broken"])
        clean_vm = _make_domain("clean-vm", ["linux-test"])
        mgr = _build_manager([broken_vm, clean_vm])

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vms_by_tags(["linux-test"], count=5)

        names = [d.name() for d in result]
        assert "broken-vm" not in names
        assert "clean-vm" in names

    def test_does_not_mutate_callers_exclude_list(self):
        """Auto-exclude should not mutate the caller's list."""
        clean_vm = _make_domain("clean-vm", ["linux-test"])
        mgr = _build_manager([clean_vm])

        original = ["used"]

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            mgr.allocate_vms(["linux-test"], count=1, task_id="t1",
                             exclude_tags=original)

        # Caller's list must not be modified
        assert original == ["used"]


# ---------------------------------------------------------------------------
# Tests for multi-host allocation
# ---------------------------------------------------------------------------

def _build_multi_host_manager(host_domains):
    """Build a VMManager with multiple mock hosts.

    Args:
        host_domains: dict of {host_name: [domain_list]}

    Returns:
        VMManager with injected mock connections for each host.
    """
    configs = {}
    connections = {}
    connected_configs = {}

    for host_name, domains in host_domains.items():
        cfg = LibvirtConnectionConfig(uri=f"qemu+ssh://{host_name}/system")
        configs[host_name] = cfg
        connections[host_name] = make_mock_conn(domains)
        connected_configs[host_name] = cfg

    mgr = VMManager(connections=configs)
    mgr._connections = connections
    mgr._connected_configs = connected_configs
    return mgr


class TestMultiHostAllocation:
    """Tests for VM allocation across multiple libvirt hosts."""

    def test_allocate_searches_both_hosts(self):
        """VMs from both hosts are candidates for allocation."""
        host1_vms = [_make_domain("host1-vm-0", ["linux-test"])]
        host2_vms = [_make_domain("host2-vm-0", ["linux-test"])]

        mgr = _build_multi_host_manager({
            "host1": host1_vms,
            "host2": host2_vms,
        })

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(["linux-test"], count=2, task_id="multi-1")

        names = [d.name() for d in result]
        assert len(result) == 2
        assert "host1-vm-0" in names
        assert "host2-vm-0" in names

    def test_allocate_respects_host_order(self):
        """First host's VMs are searched before second host's."""
        host1_vms = [_make_domain("host1-vm", ["linux-test"])]
        host2_vms = [_make_domain("host2-vm", ["linux-test"])]

        mgr = _build_multi_host_manager({
            "host1": host1_vms,
            "host2": host2_vms,
        })

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            # Request only 1 — should come from host1 (first in config order)
            result = mgr.allocate_vms(["linux-test"], count=1, task_id="order-1")

        assert len(result) == 1
        assert result[0].name() == "host1-vm"

    def test_allocate_skips_host_with_no_matches(self):
        """Host with no matching VMs is skipped, next host provides VMs."""
        host1_vms = [_make_domain("host1-vm", ["other-tag"])]
        host2_vms = [_make_domain("host2-vm", ["linux-test"])]

        mgr = _build_multi_host_manager({
            "host1": host1_vms,
            "host2": host2_vms,
        })

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(["linux-test"], count=1, task_id="skip-1")

        assert len(result) == 1
        assert result[0].name() == "host2-vm"

    def test_find_available_across_hosts(self):
        """find_available_vms_by_tags searches all hosts."""
        host1_vms = [_make_domain("h1-vm", ["linux-test"])]
        host2_vms = [
            _make_domain("h2-vm-0", ["linux-test"]),
            _make_domain("h2-vm-1", ["linux-test"]),
        ]

        mgr = _build_multi_host_manager({
            "host1": host1_vms,
            "host2": host2_vms,
        })

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False

            result = mgr.find_available_vms_by_tags(["linux-test"], count=5)

        assert len(result) == 3
        names = [d.name() for d in result]
        assert names == ["h1-vm", "h2-vm-0", "h2-vm-1"]

    def test_allocate_empty_hosts(self):
        """All hosts have zero VMs -> returns empty."""
        mgr = _build_multi_host_manager({
            "host1": [],
            "host2": [],
        })

        with patch("ansible_deployer.vm_manager.MetadataManager"):
            result = mgr.allocate_vms(["linux-test"], count=1, task_id="empty-1")

        assert result == []

    def test_broken_excluded_across_hosts(self):
        """Broken VMs are excluded regardless of which host they're on."""
        host1_vms = [_make_domain("h1-broken", ["linux-test", "broken"])]
        host2_vms = [_make_domain("h2-clean", ["linux-test"])]

        mgr = _build_multi_host_manager({
            "host1": host1_vms,
            "host2": host2_vms,
        })

        with patch("ansible_deployer.vm_manager.MetadataManager") as MockMM:
            instance = MockMM.return_value
            instance.is_in_use.return_value = False
            instance.try_claim.return_value = True

            result = mgr.allocate_vms(["linux-test"], count=2, task_id="broken-1")

        names = [d.name() for d in result]
        assert "h1-broken" not in names
        assert "h2-clean" in names
