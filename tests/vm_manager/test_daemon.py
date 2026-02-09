"""
Unit tests for VMManagerDaemon (main orchestration daemon).

Covers:
- _handle_vm_started: tag filtering + orphaned monitor prevention (Race #5)
- _should_monitor_vm: tag matching delegation
- _is_vm_actively_in_use: stale tag detection (Race #4)
- _check_existing_vms: startup scan with stale-tag filtering
- _boot_matching_vms_once: booting shutdown VMs matching filters
- Constructor and lifecycle basics
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, AsyncMock, MagicMock, call

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
        max_wait_time=300,
        check_existing=False,
        boot_at_start=False,
        boot_always=False,
        broken_tag=None,
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
            max_wait_time=600,
            check_existing=True,
            boot_at_start=True,
            boot_always=False,
            broken_tag="broken",
        )

        assert d.libvirt_uri == "qemu+ssh://host/system"
        assert d.ssh_config is cfg
        assert d.monitor_tags == ["tag-a", "tag-b"]
        # broken_tag="broken" is auto-appended to exclude_tags
        assert d.exclude_tags == ["exc", "broken"]
        assert d.tags_to_remove == ["used"]
        assert d.check_interval == 15
        assert d.max_wait_time == 600
        assert d.check_existing is True
        assert d.boot_at_start is True
        assert d.boot_always is False
        assert d.broken_tag == "broken"

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
# _is_vm_actively_in_use  (Race condition #4 coverage)
# ===================================================================

class TestIsVMActivelyInUse:
    """Tests for stale-tag detection via domain metadata."""

    def _make_metadata_xml(self, in_use=None, started_at=None):
        """Build a fake metadata XML string."""
        lines = ["<vm_info>"]
        if in_use is not None:
            lines.append(f"in_use: {'true' if in_use else 'false'}")
        if started_at is not None:
            lines.append(f"started_at: {started_at}")
        lines.append("</vm_info>")
        return "\n".join(lines)

    # -- in_use=true -> actively in use --------------------------------

    def test_in_use_true_returns_true(self):
        import libvirt as lv

        d = _make_daemon()
        domain = _make_domain("active-vm")
        domain.metadata.return_value = self._make_metadata_xml(in_use=True)

        assert d._is_vm_actively_in_use(domain) is True

    # -- started_at within 10 minutes -> actively in use ---------------

    def test_recent_started_at_returns_true(self):
        d = _make_daemon()
        domain = _make_domain("recent-vm")
        recent_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        domain.metadata.return_value = self._make_metadata_xml(
            in_use=False,
            started_at=recent_time.isoformat(),
        )

        assert d._is_vm_actively_in_use(domain) is True

    # -- started_at older than 10 minutes + not in_use -> stale --------

    def test_old_started_at_not_in_use_returns_false(self):
        d = _make_daemon()
        domain = _make_domain("old-vm")
        old_time = datetime.now(timezone.utc) - timedelta(minutes=30)
        domain.metadata.return_value = self._make_metadata_xml(
            in_use=False,
            started_at=old_time.isoformat(),
        )

        assert d._is_vm_actively_in_use(domain) is False

    # -- No metadata at all (libvirtError) -> stale --------------------

    def test_race4_no_metadata_returns_false(self):
        """
        Race condition #4 — stale tags on startup.

        VM has the 'used' tag but no metadata. This means the tag is left
        over from a previous run and should be treated as stale.
        """
        import libvirt as lv

        d = _make_daemon()
        domain = _make_domain("stale-vm")
        domain.metadata.side_effect = lv.libvirtError("no metadata")

        assert d._is_vm_actively_in_use(domain) is False

    # -- Metadata present, in_use=true -> should process ---------------

    def test_race4_in_use_true_metadata_returns_true(self):
        """
        Race condition #4 counterpart: VM has 'used' tag AND metadata
        showing in_use=true. This VM should be processed.
        """
        d = _make_daemon()
        domain = _make_domain("active-vm")
        domain.metadata.return_value = self._make_metadata_xml(in_use=True)

        assert d._is_vm_actively_in_use(domain) is True

    # -- Metadata present but in_use=false and no started_at -> stale --

    def test_metadata_no_in_use_no_started_at_returns_false(self):
        d = _make_daemon()
        domain = _make_domain("stale-vm2")
        domain.metadata.return_value = self._make_metadata_xml(in_use=False)

        assert d._is_vm_actively_in_use(domain) is False

    # -- General exception -> assume not in use ------------------------

    def test_general_exception_returns_false(self):
        d = _make_daemon()
        domain = _make_domain("error-vm")
        domain.metadata.side_effect = RuntimeError("unexpected")

        assert d._is_vm_actively_in_use(domain) is False

    # -- Empty metadata -> not in use ----------------------------------

    def test_empty_metadata_returns_false(self):
        d = _make_daemon()
        domain = _make_domain("empty-meta-vm")
        domain.metadata.return_value = "<vm_info>\n</vm_info>"

        assert d._is_vm_actively_in_use(domain) is False


# ===================================================================
# _check_existing_vms  (startup scan)
# ===================================================================

class TestCheckExistingVMs:
    """Tests for the startup scan of running VMs."""

    @pytest.mark.asyncio
    async def test_processes_matching_active_vms(self):
        """Matching + actively-in-use VMs should be forwarded to tag_cleaner."""
        d = _make_daemon()
        d.conn = Mock()
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()

        domain_a = _make_domain("vm-a", "uuid-a")
        domain_b = _make_domain("vm-b", "uuid-b")
        d.conn.listAllDomains.return_value = [domain_a, domain_b]

        with patch.object(d, "_should_monitor_vm", return_value=True), \
             patch.object(d, "_is_vm_actively_in_use", return_value=True):
            await d._check_existing_vms()

        assert d.tag_cleaner.handle_vm_started.await_count == 2
        d.tag_cleaner.handle_vm_started.assert_any_await(domain_a)
        d.tag_cleaner.handle_vm_started.assert_any_await(domain_b)

    @pytest.mark.asyncio
    async def test_skips_non_matching_vms(self):
        """VMs not matching filters should be skipped entirely."""
        d = _make_daemon()
        d.conn = Mock()
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()

        domain = _make_domain("non-matching")
        d.conn.listAllDomains.return_value = [domain]

        with patch.object(d, "_should_monitor_vm", return_value=False):
            await d._check_existing_vms()

        d.tag_cleaner.handle_vm_started.assert_not_called()

    @pytest.mark.asyncio
    async def test_race4_skips_stale_tags(self):
        """
        Race condition #4 — VMs that match filters but have stale 'used'
        tags (no metadata / not actively in use) should be skipped.
        """
        d = _make_daemon()
        d.conn = Mock()
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()

        domain = _make_domain("stale-tag-vm")
        d.conn.listAllDomains.return_value = [domain]

        with patch.object(d, "_should_monitor_vm", return_value=True), \
             patch.object(d, "_is_vm_actively_in_use", return_value=False):
            await d._check_existing_vms()

        d.tag_cleaner.handle_vm_started.assert_not_called()

    @pytest.mark.asyncio
    async def test_race4_processes_active_vm_with_metadata(self):
        """
        Race condition #4 counterpart — VM matches filters and metadata
        confirms it is actively in use. Should be processed.
        """
        d = _make_daemon()
        d.conn = Mock()
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()

        domain = _make_domain("active-vm")
        d.conn.listAllDomains.return_value = [domain]

        with patch.object(d, "_should_monitor_vm", return_value=True), \
             patch.object(d, "_is_vm_actively_in_use", return_value=True):
            await d._check_existing_vms()

        d.tag_cleaner.handle_vm_started.assert_awaited_once_with(domain)

    @pytest.mark.asyncio
    async def test_mixed_vms_only_active_matching_processed(self):
        """Only VMs that both match filters AND are active should be processed."""
        d = _make_daemon()
        d.conn = Mock()
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()

        active_matching = _make_domain("active-matching", "u1")
        stale_matching = _make_domain("stale-matching", "u2")
        non_matching = _make_domain("non-matching", "u3")

        d.conn.listAllDomains.return_value = [
            active_matching, stale_matching, non_matching,
        ]

        def mock_should_monitor(domain):
            return domain.name() != "non-matching"

        def mock_is_active(domain):
            return domain.name() == "active-matching"

        with patch.object(d, "_should_monitor_vm", side_effect=mock_should_monitor), \
             patch.object(d, "_is_vm_actively_in_use", side_effect=mock_is_active):
            await d._check_existing_vms()

        d.tag_cleaner.handle_vm_started.assert_awaited_once_with(active_matching)

    @pytest.mark.asyncio
    async def test_handles_empty_domain_list(self):
        d = _make_daemon()
        d.conn = Mock()
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()
        d.conn.listAllDomains.return_value = []

        await d._check_existing_vms()

        d.tag_cleaner.handle_vm_started.assert_not_called()

    @pytest.mark.asyncio
    async def test_continues_on_per_domain_exception(self):
        """An error on one domain should not prevent others from being checked."""
        d = _make_daemon()
        d.conn = Mock()
        d.tag_cleaner = Mock()
        d.tag_cleaner.handle_vm_started = AsyncMock()

        good_domain = _make_domain("good-vm", "u-good")
        bad_domain = _make_domain("bad-vm", "u-bad")
        d.conn.listAllDomains.return_value = [bad_domain, good_domain]

        call_count = 0

        def mock_should_monitor(domain):
            nonlocal call_count
            call_count += 1
            if domain.name() == "bad-vm":
                raise RuntimeError("libvirt error on bad-vm")
            return True

        with patch.object(d, "_should_monitor_vm", side_effect=mock_should_monitor), \
             patch.object(d, "_is_vm_actively_in_use", return_value=True):
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
