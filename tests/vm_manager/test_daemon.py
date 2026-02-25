"""
Unit tests for VMManagerDaemon (main orchestration daemon).

Covers:
- _handle_vm_started: tag filtering + orphaned monitor prevention (Race #5)
- _should_monitor_vm: tag matching delegation
- _is_vm_stale: stale tag detection via MetadataManager
- _check_existing_vms: startup scan with stale-tag cleanup
- _run_stale_tag_scan / _stale_tag_scan_loop: periodic stale tag scanning
- _boot_matching_vms_once: booting shutdown VMs matching filters
- Constructor and lifecycle basics
"""
import asyncio
from unittest.mock import Mock, patch, AsyncMock

import pytest

from vm_manager.daemon import VMManagerDaemon
from vm_manager.ssh_checker import SSHConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ssh_config():
    """Return a minimal SSHConfig for constructing daemons."""
    return SSHConfig(username="root", key_path="/test/key")


def _make_daemon(**overrides):
    """Construct a VMManagerDaemon with sensible defaults.

    Any keyword arg overrides the corresponding constructor parameter.
    """
    defaults = dict(
        libvirt_uri="qemu:///system",
        ssh_config=_make_ssh_config(),
        monitor_tags=["linux-test"],
        exclude_tags=["broken"],
        tags_to_remove=["used"],
        check_interval=10,
        check_existing=False,
        boot_at_start=False,
        boot_always=False,
        broken_tag=None,
        broken_timeout=300,
        on_broken_delay=1500,
    )
    defaults.update(overrides)
    return VMManagerDaemon(**defaults)


def _make_domain(name="test-vm", uuid="test-uuid-123"):
    """Return a mock libvirt domain."""
    domain = Mock()
    domain.name.return_value = name
    domain.UUIDString.return_value = uuid
    return domain


# ===================================================================
# Construction & attribute storage
# ===================================================================

class TestVMManagerDaemonInit:
    """Verify that the constructor stores all parameters correctly."""

    def test_stores_all_parameters(self):
        cfg = _make_ssh_config()
        d = VMManagerDaemon(
            libvirt_uri="qemu+ssh://host/system",
            ssh_config=cfg,
            monitor_tags=["tag-a", "tag-b"],
            exclude_tags=["exc"],
            tags_to_remove=["used"],
            check_interval=15,
            check_existing=True,
            boot_at_start=True,
            boot_always=False,
            broken_tag="broken",
            broken_timeout=600,
            on_broken_delay=900,
        )

        assert d.libvirt_uri == "qemu+ssh://host/system"
        assert d.ssh_config is cfg
        assert d.monitor_tags == ["tag-a", "tag-b"]
        # broken_tag="broken" is auto-appended to exclude_tags
        assert d.exclude_tags == ["exc", "broken"]
        assert d.tags_to_remove == ["used"]
        assert d.check_interval == 15
        assert d.check_existing is True
        assert d.boot_at_start is True
        assert d.boot_always is False
        assert d.broken_tag == "broken"
        assert d.broken_timeout == 600
        assert d.on_broken_delay == 900

    def test_default_broken_tag_is_none(self):
        d = _make_daemon()
        assert d.broken_tag is None

    def test_components_are_none_before_start(self):
        d = _make_daemon()
        assert d.conn is None
        assert d.ssh_checker is None
        assert d.vm_tracker is None
        assert d.event_monitor is None
        assert d.tag_cleaner is None

    def test_not_running_before_start(self):
        d = _make_daemon()
        assert d._running is False

    def test_auto_excludes_broken_tag(self):
        """When broken_tag is set, it is auto-appended to exclude_tags."""
        d = _make_daemon(broken_tag="broken", exclude_tags=[])
        assert "broken" in d.exclude_tags

    def test_auto_excludes_custom_broken_tag(self):
        """Auto-exclude works with custom broken tag names."""
        d = _make_daemon(broken_tag="needs-repair", exclude_tags=["other"])
        assert "needs-repair" in d.exclude_tags
        assert "other" in d.exclude_tags

    def test_no_duplicate_when_broken_tag_already_excluded(self):
        """If broken_tag is already in exclude_tags, don't add it again."""
        d = _make_daemon(broken_tag="broken", exclude_tags=["broken"])
        assert d.exclude_tags.count("broken") == 1

    def test_no_auto_exclude_when_broken_tag_is_none(self):
        """When broken_tag is None, exclude_tags is not modified."""
        d = _make_daemon(broken_tag=None, exclude_tags=["manual"])
        assert d.exclude_tags == ["manual"]

    def test_does_not_mutate_callers_list(self):
        """Auto-exclude should not mutate the original list passed to constructor."""
        original = ["other"]
        d = _make_daemon(broken_tag="broken", exclude_tags=original)
        # The daemon should have copied the list
        assert "broken" not in original
        assert "broken" in d.exclude_tags

    def test_stores_on_broken(self):
        """Constructor stores on_broken path."""
        d = _make_daemon(on_broken="/path/to/handler.sh")
        assert d.on_broken == "/path/to/handler.sh"

    def test_default_on_broken_is_none(self):
        d = _make_daemon()
        assert d.on_broken is None

    def test_stores_on_broken_timeout(self):
        d = _make_daemon(on_broken_timeout=600)
        assert d.on_broken_timeout == 600

    def test_default_on_broken_timeout(self):
        d = _make_daemon()
        assert d.on_broken_timeout == 300

    def test_stores_on_broken_retries(self):
        d = _make_daemon(on_broken_retries=5)
        assert d.on_broken_retries == 5

    def test_default_on_broken_retries_is_none(self):
        d = _make_daemon()
        assert d.on_broken_retries is None

    def test_stores_on_broken_retry_delay(self):
        d = _make_daemon(on_broken_retry_delay=120)
        assert d.on_broken_retry_delay == 120

    def test_default_on_broken_retry_delay(self):
        d = _make_daemon()
        assert d.on_broken_retry_delay == 60

    def test_stores_broken_timeout(self):
        d = _make_daemon(broken_timeout=600)
        assert d.broken_timeout == 600

    def test_default_broken_timeout(self):
        d = _make_daemon()
        assert d.broken_timeout == 300

    def test_stores_on_broken_delay(self):
        d = _make_daemon(on_broken_delay=900)
        assert d.on_broken_delay == 900

    def test_default_on_broken_delay(self):
        d = _make_daemon()
        assert d.on_broken_delay == 1500

    def test_stores_stale_scan_interval(self):
        d = _make_daemon(stale_scan_interval=120)
        assert d.stale_scan_interval == 120

    def test_default_stale_scan_interval(self):
        d = _make_daemon()
        assert d.stale_scan_interval == 300


# ===================================================================
# _should_monitor_vm
# ===================================================================

class TestShouldMonitorVM:
    """Tests for the tag-filter check delegated to get_vm_tags + vm_matches_tags."""

    @patch("vm_manager.daemon.vm_matches_tags")
    @patch("vm_manager.daemon.get_vm_tags")
    def test_returns_true_when_tags_match(self, mock_get_tags, mock_matches):
        mock_get_tags.return_value = ["linux-test", "used"]
        mock_matches.return_value = True

        d = _make_daemon(monitor_tags=["linux-test"], exclude_tags=["broken"])
        domain = _make_domain()

        assert d._should_monitor_vm(domain) is True
        mock_get_tags.assert_called_once_with(domain)
        mock_matches.assert_called_once_with(
            vm_tags=["linux-test", "used"],
            required_tags=["linux-test"],
            exclude_tags=["broken"],
        )

    @patch("vm_manager.daemon.vm_matches_tags")
    @patch("vm_manager.daemon.get_vm_tags")
    def test_returns_false_when_tags_do_not_match(self, mock_get_tags, mock_matches):
        mock_get_tags.return_value = ["other-tag"]
        mock_matches.return_value = False

        d = _make_daemon(monitor_tags=["linux-test"], exclude_tags=["broken"])
        domain = _make_domain()

        assert d._should_monitor_vm(domain) is False

    @patch("vm_manager.daemon.vm_matches_tags")
    @patch("vm_manager.daemon.get_vm_tags")
    def test_returns_false_when_vm_has_excluded_tag(self, mock_get_tags, mock_matches):
        mock_get_tags.return_value = ["linux-test", "broken"]
        mock_matches.return_value = False  # vm_matches_tags rejects it

        d = _make_daemon(monitor_tags=["linux-test"], exclude_tags=["broken"])
        domain = _make_domain()

        assert d._should_monitor_vm(domain) is False

    @patch("vm_manager.daemon.get_vm_tags", side_effect=Exception("libvirt error"))
    def test_returns_false_on_exception(self, _mock):
        d = _make_daemon()
        domain = _make_domain()

        assert d._should_monitor_vm(domain) is False


# ===================================================================
# _handle_vm_started  (Race condition #5 coverage)
# ===================================================================

class TestHandleVMStarted:
    """Tests for the VM-started callback, including orphaned-monitor prevention."""

    def _setup_daemon_with_tag_cleaner(self, **overrides):
        """Build a daemon with a mocked tag_cleaner already attached."""
        d = _make_daemon(**overrides)
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()
        return d

    # -- Happy path: VM matches filters AND has removable tags --------

    @patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test", "used"])
    @patch("vm_manager.daemon.vm_matches_tags", return_value=True)
    def test_starts_monitoring_when_all_checks_pass(self, _m_matches, _m_tags):
        d = self._setup_daemon_with_tag_cleaner(
            monitor_tags=["linux-test"],
            tags_to_remove=["used"],
        )
        domain = _make_domain("my-vm")

        # Run in an event loop so asyncio.create_task works
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._call_handle_vm_started(d, domain))
        finally:
            loop.close()

        d.tag_cleaner.handle_vm_started.assert_awaited_once_with(domain)

    @staticmethod
    async def _call_handle_vm_started(daemon, domain):
        """Call the synchronous callback and let spawned tasks run."""
        daemon._handle_vm_started(domain)
        # Let the created task actually execute
        await asyncio.sleep(0)

    # -- Race #5: VM matches filters but does NOT have removable tags --

    @patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test"])
    @patch("vm_manager.daemon.vm_matches_tags", return_value=True)
    def test_race5_skips_vm_without_removable_tags(self, _m_matches, _m_tags):
        """
        Race condition #5 — orphaned monitors.

        Scenario: VM reboots after tag removal. The 'used' tag has already
        been removed so tags_to_remove are absent. _handle_vm_started must
        detect this and NOT start a monitor.
        """
        d = self._setup_daemon_with_tag_cleaner(
            monitor_tags=["linux-test"],
            tags_to_remove=["used"],
        )
        domain = _make_domain("rebooted-vm")

        d._handle_vm_started(domain)

        d.tag_cleaner.handle_vm_started.assert_not_called()

    @patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test", "used"])
    @patch("vm_manager.daemon.vm_matches_tags", return_value=True)
    def test_race5_processes_vm_with_removable_tags(self, _m_matches, _m_tags):
        """
        Counterpart to race #5: if the VM still has 'used', monitoring must proceed.
        """
        d = self._setup_daemon_with_tag_cleaner(
            monitor_tags=["linux-test"],
            tags_to_remove=["used"],
        )
        domain = _make_domain("fresh-vm")

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._call_handle_vm_started(d, domain))
        finally:
            loop.close()

        d.tag_cleaner.handle_vm_started.assert_awaited_once_with(domain)

    # -- Multiple removable tags: any one present is enough -----------

    @patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test", "provisioning"])
    @patch("vm_manager.daemon.vm_matches_tags", return_value=True)
    def test_processes_vm_with_any_one_removable_tag(self, _m_matches, _m_tags):
        d = self._setup_daemon_with_tag_cleaner(
            monitor_tags=["linux-test"],
            tags_to_remove=["used", "provisioning"],
        )
        domain = _make_domain("multi-tag-vm")

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._call_handle_vm_started(d, domain))
        finally:
            loop.close()

        d.tag_cleaner.handle_vm_started.assert_awaited_once_with(domain)

    # -- Filter mismatch: VM does not match monitor_tags at all -------

    @patch("vm_manager.daemon.get_vm_tags", return_value=["unrelated"])
    @patch("vm_manager.daemon.vm_matches_tags", return_value=False)
    def test_skips_vm_not_matching_filters(self, _m_matches, _m_tags):
        d = self._setup_daemon_with_tag_cleaner()
        domain = _make_domain("unrelated-vm")

        d._handle_vm_started(domain)

        d.tag_cleaner.handle_vm_started.assert_not_called()

    # -- Exception safety ---------------------------------------------

    @patch("vm_manager.daemon.get_vm_tags", side_effect=Exception("kaboom"))
    @patch("vm_manager.daemon.vm_matches_tags", return_value=True)
    def test_exception_in_get_vm_tags_does_not_propagate(self, _m_matches, _m_tags):
        """_handle_vm_started catches all exceptions to protect the event loop."""
        d = self._setup_daemon_with_tag_cleaner()
        domain = _make_domain("broken-vm")

        # Should not raise
        d._handle_vm_started(domain)
        d.tag_cleaner.handle_vm_started.assert_not_called()

    @patch("vm_manager.daemon.vm_matches_tags", return_value=False)
    @patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test"])
    def test_should_monitor_returns_false_prevents_tag_check(self, _m_tags, _m_matches):
        """When _should_monitor_vm returns False, get_vm_tags for removable-tag
        check should NOT be reached (we return early)."""
        d = self._setup_daemon_with_tag_cleaner()
        domain = _make_domain()

        d._handle_vm_started(domain)

        d.tag_cleaner.handle_vm_started.assert_not_called()
        # get_vm_tags is called once inside _should_monitor_vm, but we
        # should NOT see a second call for the removable-tag check.
        # The first call comes from _should_monitor_vm -> get_vm_tags.
        # The second would come from the removable tag guard.
        # Since _should_monitor_vm returned False, we returned early.


# ===================================================================
# _is_vm_stale  (stale tag detection)
# ===================================================================

class TestIsVMStale:
    """Tests for stale-tag detection via MetadataManager."""

    @patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test", "used"])
    def test_stale_when_not_in_use(self, _mock_tags):
        """VM with 'used' tag and in_use=false should be stale."""
        d = _make_daemon()
        domain = _make_domain("stale-vm")

        with patch("vm_manager.daemon.MetadataManager") as MockMM:
            MockMM.return_value.is_in_use.return_value = False
            assert d._is_vm_stale(domain) is True

    @patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test", "used"])
    def test_not_stale_when_in_use(self, _mock_tags):
        """VM with 'used' tag and in_use=true should NOT be stale."""
        d = _make_daemon()
        domain = _make_domain("active-vm")

        with patch("vm_manager.daemon.MetadataManager") as MockMM:
            MockMM.return_value.is_in_use.return_value = True
            assert d._is_vm_stale(domain) is False

    @patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test"])
    def test_not_stale_when_no_removable_tags(self, _mock_tags):
        """VM without any removable tags should NOT be stale."""
        d = _make_daemon()
        domain = _make_domain("clean-vm")

        assert d._is_vm_stale(domain) is False

    @patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test", "used"])
    def test_stale_when_no_metadata(self, _mock_tags):
        """VM with 'used' tag but no metadata (exception) should be stale."""
        d = _make_daemon()
        domain = _make_domain("no-meta-vm")

        with patch("vm_manager.daemon.MetadataManager") as MockMM:
            MockMM.return_value.is_in_use.side_effect = Exception("no metadata")
            assert d._is_vm_stale(domain) is True

    @patch("vm_manager.daemon.get_vm_tags", side_effect=Exception("libvirt error"))
    def test_returns_false_on_exception(self, _mock_tags):
        """General exception should return False (not stale)."""
        d = _make_daemon()
        domain = _make_domain("error-vm")

        assert d._is_vm_stale(domain) is False


# ===================================================================
# _check_existing_vms  (startup scan)
# ===================================================================

class TestCheckExistingVMs:
    """Tests for the startup scan of running VMs.

    _check_existing_vms routes ALL VMs with removable tags through SSH
    monitoring (handle_vm_started), regardless of whether they are stale
    (in_use=false) or actively in use.  This ensures broken VMs are always
    detected via SSH timeout rather than having tags silently removed.
    """

    def _setup(self, **overrides):
        d = _make_daemon(**overrides)
        d.conn = Mock()
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()
        return d

    @pytest.mark.asyncio
    async def test_vms_with_removable_tags_get_ssh_monitoring(self):
        """Matching VMs with removable tags go through SSH-wait."""
        d = self._setup()

        domain_a = _make_domain("vm-a", "uuid-a")
        domain_b = _make_domain("vm-b", "uuid-b")
        d.conn.listAllDomains.return_value = [domain_a, domain_b]

        with patch.object(d, "_should_monitor_vm", return_value=True), \
             patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test", "used"]):
            await d._check_existing_vms()

        assert d.tag_cleaner.handle_vm_started.await_count == 2
        d.tag_cleaner.handle_vm_started.assert_any_await(domain_a)
        d.tag_cleaner.handle_vm_started.assert_any_await(domain_b)

    @pytest.mark.asyncio
    async def test_stale_vms_also_get_ssh_monitoring(self):
        """Stale VMs (in_use=false) also go through SSH monitoring, not direct removal.

        This is the Bug 1 fix: previously stale VMs had tags removed directly
        without SSH verification, allowing broken VMs to escape detection.
        """
        d = self._setup()

        domain = _make_domain("stale-tag-vm")
        d.conn.listAllDomains.return_value = [domain]

        with patch.object(d, "_should_monitor_vm", return_value=True), \
             patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test", "used"]):
            await d._check_existing_vms()

        d.tag_cleaner.handle_vm_started.assert_awaited_once_with(domain)

    @pytest.mark.asyncio
    async def test_skips_non_matching_vms(self):
        """VMs not matching filters should be skipped entirely."""
        d = self._setup()

        domain = _make_domain("non-matching")
        d.conn.listAllDomains.return_value = [domain]

        with patch.object(d, "_should_monitor_vm", return_value=False):
            await d._check_existing_vms()

        d.tag_cleaner.handle_vm_started.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_vms_without_removable_tags(self):
        """Matching VMs with NO removable tags should be skipped."""
        d = self._setup()

        domain = _make_domain("clean-vm")
        d.conn.listAllDomains.return_value = [domain]

        with patch.object(d, "_should_monitor_vm", return_value=True), \
             patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test"]):
            await d._check_existing_vms()

        d.tag_cleaner.handle_vm_started.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_vms_correct_routing(self):
        """VMs with removable tags get SSH monitoring; non-matching and clean VMs skipped."""
        d = self._setup()

        active_vm = _make_domain("active-vm", "u1")
        stale_vm = _make_domain("stale-vm", "u2")
        non_matching = _make_domain("non-matching", "u3")
        clean_vm = _make_domain("clean-vm", "u4")

        d.conn.listAllDomains.return_value = [active_vm, stale_vm, non_matching, clean_vm]

        def mock_should_monitor(domain):
            return domain.name() != "non-matching"

        def mock_get_tags(domain):
            if domain.name() == "clean-vm":
                return ["linux-test"]  # no removable tags
            return ["linux-test", "used"]

        with patch.object(d, "_should_monitor_vm", side_effect=mock_should_monitor), \
             patch("vm_manager.daemon.get_vm_tags", side_effect=mock_get_tags):
            await d._check_existing_vms()

        # Both active_vm and stale_vm have removable tags → SSH monitoring
        assert d.tag_cleaner.handle_vm_started.await_count == 2
        d.tag_cleaner.handle_vm_started.assert_any_await(active_vm)
        d.tag_cleaner.handle_vm_started.assert_any_await(stale_vm)

    @pytest.mark.asyncio
    async def test_handles_empty_domain_list(self):
        d = self._setup()
        d.conn.listAllDomains.return_value = []

        await d._check_existing_vms()

        d.tag_cleaner.handle_vm_started.assert_not_called()

    @pytest.mark.asyncio
    async def test_continues_on_per_domain_exception(self):
        """An error on one domain should not prevent others from being checked."""
        d = self._setup()

        good_domain = _make_domain("good-vm", "u-good")
        bad_domain = _make_domain("bad-vm", "u-bad")
        d.conn.listAllDomains.return_value = [bad_domain, good_domain]

        def mock_should_monitor(domain):
            if domain.name() == "bad-vm":
                raise RuntimeError("libvirt error on bad-vm")
            return True

        with patch.object(d, "_should_monitor_vm", side_effect=mock_should_monitor), \
             patch("vm_manager.daemon.get_vm_tags", return_value=["linux-test", "used"]):
            await d._check_existing_vms()

        # The good domain should still have been processed
        d.tag_cleaner.handle_vm_started.assert_awaited_once_with(good_domain)

    @pytest.mark.asyncio
    async def test_handles_listAllDomains_exception(self):
        """Failure to list domains should not crash the daemon."""
        d = _make_daemon()
        d.conn = Mock()
        d.conn.listAllDomains.side_effect = Exception("connection lost")

        # Should not raise
        await d._check_existing_vms()


# ===================================================================
# _boot_matching_vms_once
# ===================================================================

class TestBootMatchingVMsOnce:
    """Tests for the boot-at-start functionality."""

    @pytest.mark.asyncio
    async def test_boots_matching_shutdown_vms(self):
        d = _make_daemon()
        d.conn = Mock()

        domain_a = _make_domain("vm-a")
        domain_b = _make_domain("vm-b")
        d.conn.listAllDomains.return_value = [domain_a, domain_b]

        with patch.object(d, "_should_monitor_vm", return_value=True):
            await d._boot_matching_vms_once()

        domain_a.create.assert_called_once()
        domain_b.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_non_matching_shutdown_vms(self):
        d = _make_daemon()
        d.conn = Mock()

        matching = _make_domain("matching-vm")
        non_matching = _make_domain("other-vm")
        d.conn.listAllDomains.return_value = [matching, non_matching]

        def side_effect(domain):
            return domain.name() == "matching-vm"

        with patch.object(d, "_should_monitor_vm", side_effect=side_effect):
            await d._boot_matching_vms_once()

        matching.create.assert_called_once()
        non_matching.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_empty_domain_list(self):
        d = _make_daemon()
        d.conn = Mock()
        d.conn.listAllDomains.return_value = []

        await d._boot_matching_vms_once()
        # No error, nothing to assert beyond no exceptions

    @pytest.mark.asyncio
    async def test_continues_on_boot_failure(self):
        """Failure to boot one VM should not prevent booting others."""
        d = _make_daemon()
        d.conn = Mock()

        bad_vm = _make_domain("bad-vm")
        bad_vm.create.side_effect = Exception("boot failed")
        good_vm = _make_domain("good-vm")
        d.conn.listAllDomains.return_value = [bad_vm, good_vm]

        with patch.object(d, "_should_monitor_vm", return_value=True):
            await d._boot_matching_vms_once()

        bad_vm.create.assert_called_once()
        good_vm.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_listAllDomains_uses_shutoff_flag(self):
        """Verify we query only shut-off domains, not running ones."""
        import libvirt as lv

        d = _make_daemon()
        d.conn = Mock()
        d.conn.listAllDomains.return_value = []

        await d._boot_matching_vms_once()

        d.conn.listAllDomains.assert_called_once_with(
            lv.VIR_CONNECT_LIST_DOMAINS_SHUTOFF
        )

    @pytest.mark.asyncio
    async def test_handles_listAllDomains_exception(self):
        d = _make_daemon()
        d.conn = Mock()
        d.conn.listAllDomains.side_effect = Exception("conn lost")

        # Should not raise
        await d._boot_matching_vms_once()


# ===================================================================
# _handle_vm_stopped
# ===================================================================

class TestHandleVMStopped:
    """Tests for the VM-stopped callback (used in boot-always mode)."""

    def test_does_not_raise_on_normal_domain(self):
        d = _make_daemon(boot_always=True)
        domain = _make_domain("stopped-vm")

        # Should not raise
        d._handle_vm_stopped(domain)

    def test_handles_exception_in_domain_name(self):
        d = _make_daemon(boot_always=True)
        domain = Mock()
        domain.name.side_effect = Exception("libvirt gone")

        # Should not raise
        d._handle_vm_stopped(domain)


# ===================================================================
# shutdown / lifecycle
# ===================================================================

class TestShutdown:
    """Tests for the shutdown mechanism."""

    def test_shutdown_sets_event(self):
        d = _make_daemon()
        assert not d._shutdown_event.is_set()
        d.shutdown()
        assert d._shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_run_returns_after_shutdown(self):
        d = _make_daemon()
        d._running = True

        # Schedule shutdown after a tiny delay
        async def _trigger_shutdown():
            await asyncio.sleep(0.01)
            d.shutdown()

        task = asyncio.create_task(_trigger_shutdown())
        await d.run()
        await task  # Ensure the trigger task completes cleanly

    @pytest.mark.asyncio
    async def test_run_returns_immediately_if_not_started(self):
        d = _make_daemon()
        # _running is False, run() should return immediately
        await d.run()

    @pytest.mark.asyncio
    async def test_stop_is_idempotent_when_not_running(self):
        d = _make_daemon()
        # Should not raise even though nothing is initialized
        await d.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_tracker_and_closes_conn(self):
        d = _make_daemon()
        d._running = True
        d.event_monitor = Mock()
        d.event_monitor.stop = AsyncMock()
        d.vm_tracker = Mock()
        d.vm_tracker.cancel_all = AsyncMock()
        d.conn = Mock()

        await d.stop()

        d.event_monitor.stop.assert_awaited_once()
        d.vm_tracker.cancel_all.assert_awaited_once()
        d.conn.close.assert_called_once()
        assert d._running is False

    @pytest.mark.asyncio
    async def test_stop_handles_conn_close_exception(self):
        d = _make_daemon()
        d._running = True
        d.event_monitor = Mock()
        d.event_monitor.stop = AsyncMock()
        d.vm_tracker = Mock()
        d.vm_tracker.cancel_all = AsyncMock()
        d.conn = Mock()
        d.conn.close.side_effect = Exception("already closed")

        # Should not raise
        await d.stop()
        assert d._running is False


# ===================================================================
# _run_stale_tag_scan / _stale_tag_scan_loop
# ===================================================================

class TestRunStaleScan:
    """Tests for the periodic stale tag scan.

    The stale scan now routes VMs through SSH monitoring (handle_vm_started)
    instead of removing tags directly.  This ensures broken VMs are detected
    via SSH timeout and handled by the --on-broken mechanism.
    """

    def _setup(self, stale_scan_interval=300, **overrides):
        d = _make_daemon(stale_scan_interval=stale_scan_interval, **overrides)
        d.conn = Mock()
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()
        d.vm_tracker = Mock()
        d.vm_tracker.is_monitoring = AsyncMock(return_value=False)
        d._running = True
        return d

    @pytest.mark.asyncio
    async def test_stale_vms_get_ssh_monitoring(self):
        """Stale VMs should be routed through SSH monitoring."""
        d = self._setup()

        domain = _make_domain("stale-vm", "uuid-stale")
        d.conn.listAllDomains.return_value = [domain]

        with patch.object(d, "_should_monitor_vm", return_value=True), \
             patch.object(d, "_is_vm_stale", return_value=True):
            await d._run_stale_tag_scan()

        d.tag_cleaner.handle_vm_started.assert_awaited_once_with(domain)

    @pytest.mark.asyncio
    async def test_skips_non_matching_vms(self):
        """VMs not matching tag filters should be skipped."""
        d = self._setup()

        domain = _make_domain("unrelated-vm", "uuid-unrel")
        d.conn.listAllDomains.return_value = [domain]

        with patch.object(d, "_should_monitor_vm", return_value=False):
            await d._run_stale_tag_scan()

        d.tag_cleaner.handle_vm_started.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_currently_monitored_vms(self):
        """VMs already being monitored (SSH-wait in progress) should be skipped."""
        d = self._setup()
        d.vm_tracker.is_monitoring = AsyncMock(return_value=True)

        domain = _make_domain("monitored-vm", "uuid-monitored")
        d.conn.listAllDomains.return_value = [domain]

        with patch.object(d, "_should_monitor_vm", return_value=True), \
             patch.object(d, "_is_vm_stale", return_value=True):
            await d._run_stale_tag_scan()

        d.tag_cleaner.handle_vm_started.assert_not_called()
        d.vm_tracker.is_monitoring.assert_awaited_once_with("uuid-monitored")

    @pytest.mark.asyncio
    async def test_skips_non_stale_vms(self):
        """VMs that are not stale (actively in use) should be skipped."""
        d = self._setup()

        domain = _make_domain("active-vm", "uuid-active")
        d.conn.listAllDomains.return_value = [domain]

        with patch.object(d, "_should_monitor_vm", return_value=True), \
             patch.object(d, "_is_vm_stale", return_value=False):
            await d._run_stale_tag_scan()

        d.tag_cleaner.handle_vm_started.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_vms_only_stale_monitored(self):
        """Only stale, matching, non-monitored VMs should get SSH monitoring."""
        d = self._setup()

        stale_vm = _make_domain("stale-vm", "uuid-stale")
        active_vm = _make_domain("active-vm", "uuid-active")
        monitored_vm = _make_domain("monitored-vm", "uuid-monitored")
        non_matching = _make_domain("non-matching", "uuid-nonmatch")

        d.conn.listAllDomains.return_value = [
            stale_vm, active_vm, monitored_vm, non_matching
        ]

        def mock_should_monitor(domain):
            return domain.name() != "non-matching"

        def mock_is_stale(domain):
            return domain.name() in ("stale-vm", "monitored-vm")

        async def mock_is_monitoring(uuid):
            return uuid == "uuid-monitored"

        d.vm_tracker.is_monitoring = AsyncMock(side_effect=mock_is_monitoring)

        with patch.object(d, "_should_monitor_vm", side_effect=mock_should_monitor), \
             patch.object(d, "_is_vm_stale", side_effect=mock_is_stale):
            await d._run_stale_tag_scan()

        d.tag_cleaner.handle_vm_started.assert_awaited_once_with(stale_vm)

    @pytest.mark.asyncio
    async def test_handles_listAllDomains_exception(self):
        """Failure to list domains should not crash the scan."""
        d = self._setup()
        d.conn.listAllDomains.side_effect = Exception("connection lost")

        # Should not raise
        await d._run_stale_tag_scan()

    @pytest.mark.asyncio
    async def test_continues_on_per_domain_exception(self):
        """An error on one domain should not prevent scanning others."""
        d = self._setup()

        bad_domain = _make_domain("bad-vm", "uuid-bad")
        good_domain = _make_domain("good-vm", "uuid-good")
        d.conn.listAllDomains.return_value = [bad_domain, good_domain]

        def mock_should_monitor(domain):
            if domain.name() == "bad-vm":
                raise RuntimeError("libvirt error")
            return True

        with patch.object(d, "_should_monitor_vm", side_effect=mock_should_monitor), \
             patch.object(d, "_is_vm_stale", return_value=True):
            await d._run_stale_tag_scan()

        d.tag_cleaner.handle_vm_started.assert_awaited_once_with(good_domain)


class TestStaleScanLoop:
    """Tests for the stale scan loop lifecycle."""

    @pytest.mark.asyncio
    async def test_loop_runs_scan_after_interval(self):
        """The loop should call _run_stale_tag_scan after sleeping."""
        d = _make_daemon(stale_scan_interval=60)  # value doesn't matter, we mock sleep
        d._running = True

        scan_call_count = 0

        async def mock_scan():
            nonlocal scan_call_count
            scan_call_count += 1
            # Stop the loop after first scan
            d._running = False

        original_sleep = asyncio.sleep

        async def mock_sleep(seconds):
            # Only delay a tiny bit
            await original_sleep(0.001)

        with patch.object(d, "_run_stale_tag_scan", side_effect=mock_scan), \
             patch("asyncio.sleep", side_effect=mock_sleep):
            await d._stale_tag_scan_loop()

        assert scan_call_count == 1

    @pytest.mark.asyncio
    async def test_loop_handles_scan_exception(self):
        """Exceptions in the scan should not stop the loop."""
        d = _make_daemon(stale_scan_interval=60)
        d._running = True

        scan_call_count = 0

        async def mock_scan():
            nonlocal scan_call_count
            scan_call_count += 1
            if scan_call_count == 1:
                raise RuntimeError("scan failed")
            # Stop after second call
            d._running = False

        original_sleep = asyncio.sleep

        async def mock_sleep(seconds):
            await original_sleep(0.001)

        with patch.object(d, "_run_stale_tag_scan", side_effect=mock_scan), \
             patch("asyncio.sleep", side_effect=mock_sleep):
            await d._stale_tag_scan_loop()

        assert scan_call_count == 2  # First failed, second succeeded and stopped

    @pytest.mark.asyncio
    async def test_loop_stops_when_not_running(self):
        """The loop should exit when _running is set to False."""
        d = _make_daemon(stale_scan_interval=60)
        d._running = False  # Not running from the start

        with patch.object(d, "_run_stale_tag_scan", new_callable=AsyncMock) as mock_scan:
            original_sleep = asyncio.sleep

            async def mock_sleep(seconds):
                await original_sleep(0.001)

            with patch("asyncio.sleep", side_effect=mock_sleep):
                await d._stale_tag_scan_loop()

            mock_scan.assert_not_called()


# ===================================================================
# _check_existing_vms uses VIR_CONNECT_LIST_DOMAINS_RUNNING
# ===================================================================

class TestCheckExistingVMsFlag:

    @pytest.mark.asyncio
    async def test_queries_running_domains(self):
        """Startup scan must query only running VMs."""
        import libvirt as lv

        d = _make_daemon()
        d.conn = Mock()
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()
        d.conn.listAllDomains.return_value = []

        await d._check_existing_vms()

        d.conn.listAllDomains.assert_called_once_with(
            lv.VIR_CONNECT_LIST_DOMAINS_RUNNING
        )


# ===================================================================
# Integration-style: _handle_vm_started get_vm_tags call ordering
# ===================================================================

class TestHandleVMStartedCallOrdering:
    """Verify that get_vm_tags is called in the right order and context."""

    @patch("vm_manager.daemon.get_vm_tags")
    @patch("vm_manager.daemon.vm_matches_tags", return_value=True)
    def test_get_vm_tags_called_twice_when_filter_passes(self, _m_matches, m_tags):
        """
        get_vm_tags is called once in _should_monitor_vm and once in the
        removable-tag guard. Both calls receive the domain.
        """
        m_tags.return_value = ["linux-test", "used"]

        d = _make_daemon(monitor_tags=["linux-test"], tags_to_remove=["used"])
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()
        domain = _make_domain()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._call(d, domain))
        finally:
            loop.close()

        # Called once in _should_monitor_vm and once in the guard
        assert m_tags.call_count == 2
        m_tags.assert_called_with(domain)

    @patch("vm_manager.daemon.get_vm_tags")
    @patch("vm_manager.daemon.vm_matches_tags", return_value=False)
    def test_get_vm_tags_called_once_when_filter_fails(self, _m_matches, m_tags):
        """If the filter rejects the VM, we return early — only one call."""
        m_tags.return_value = ["unrelated"]

        d = _make_daemon(monitor_tags=["linux-test"], tags_to_remove=["used"])
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()
        domain = _make_domain()

        d._handle_vm_started(domain)

        assert m_tags.call_count == 1

    @staticmethod
    async def _call(daemon, domain):
        daemon._handle_vm_started(domain)
        await asyncio.sleep(0)
