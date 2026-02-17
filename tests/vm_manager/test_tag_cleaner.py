"""
Unit tests for TagCleaner (orchestration of SSH checks and tag removal).
"""
import pytest
import asyncio
from unittest.mock import Mock, patch, AsyncMock, MagicMock
from vm_manager.tag_cleaner import TagCleaner
from vm_manager.ssh_checker import SSHChecker, SSHConfig
from vm_manager.vm_tracker import VMTracker


class TestTagCleaner:
    """Test TagCleaner orchestration logic."""
    
    @pytest.fixture
    def mock_conn(self):
        """Create a mock libvirt connection."""
        return Mock()
    
    @pytest.fixture
    def mock_domain(self):
        """Create a mock libvirt domain."""
        domain = Mock()
        domain.name.return_value = "test-vm"
        domain.UUIDString.return_value = "test-uuid-123"
        return domain
    
    @pytest.fixture
    def ssh_checker(self):
        """Create a mock SSH checker."""
        config = SSHConfig(username="root", key_path="/test/key")
        return SSHChecker(config)
    
    @pytest.fixture
    def vm_tracker(self):
        """Create a real VM tracker."""
        return VMTracker()
    
    @pytest.fixture
    def tag_cleaner(self, mock_conn, ssh_checker, vm_tracker):
        """Create a TagCleaner instance."""
        return TagCleaner(
            conn=mock_conn,
            ssh_checker=ssh_checker,
            vm_tracker=vm_tracker,
            tags_to_remove=["test-tag"]
        )
    
    @pytest.mark.asyncio
    async def test_get_vm_ip_with_retry_success_first_try(self, tag_cleaner, mock_domain, mock_conn):
        """Test getting VM IP succeeds on first try."""
        mock_conn.lookupByName.return_value = mock_domain
        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="192.168.1.100"):
            ip = await tag_cleaner._get_vm_ip_with_retry("test-vm")
            
            assert ip == "192.168.1.100"
            mock_conn.lookupByName.assert_called_with("test-vm")
    
    @pytest.mark.asyncio
    async def test_get_vm_ip_with_retry_after_retries(self, tag_cleaner, mock_domain, mock_conn):
        """Test getting VM IP succeeds after retries."""
        mock_conn.lookupByName.return_value = mock_domain
        call_count = 0
        
        def mock_get_ip(domain, network):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return None  # No IP yet
            return "192.168.1.100"
        
        with patch('vm_manager.tag_cleaner.get_vm_ip', side_effect=mock_get_ip):
            ip = await tag_cleaner._get_vm_ip_with_retry(
                "test-vm",
                max_attempts=5,
                retry_interval=0.1
            )
            
            assert ip == "192.168.1.100"
            assert call_count == 3
    
    @pytest.mark.asyncio
    async def test_get_vm_ip_with_retry_timeout(self, tag_cleaner, mock_domain, mock_conn):
        """Test getting VM IP times out after max attempts."""
        mock_conn.lookupByName.return_value = mock_domain
        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value=None):
            ip = await tag_cleaner._get_vm_ip_with_retry(
                "test-vm",
                max_attempts=3,
                retry_interval=0.1
            )
            
            assert ip is None
    
    @pytest.mark.asyncio
    async def test_monitor_vm_success(self, tag_cleaner, mock_domain, mock_conn):
        """Test successful VM monitoring: IP -> SSH -> tag removal."""
        mock_conn.lookupByName.return_value = mock_domain
        
        # Mock getting IP
        async def mock_ssh_success(*args, **kwargs):
            return "success"
        
        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="192.168.1.100"), \
             patch.object(tag_cleaner.ssh_checker, 'wait_for_ssh', side_effect=mock_ssh_success), \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove_tag, \
             patch.object(tag_cleaner, '_is_vm_in_use', return_value=False), \
             patch('asyncio.sleep', new_callable=AsyncMock):
            
            await tag_cleaner._monitor_vm(
                "test-uuid-123",
                "test-vm"
            )
            
            # Verify domain was looked up and tag was removed
            mock_conn.lookupByName.assert_called_with("test-vm")
            mock_remove_tag.assert_called_once_with(
                mock_conn,
                mock_domain,
                "test-tag"
            )
    
    @pytest.mark.asyncio
    async def test_monitor_vm_no_ip_marks_broken(
        self, mock_conn, vm_tracker, mock_domain
    ):
        """When max_wait_time is set and no IP is ever found, VM is marked broken."""
        mock_conn.lookupByName.return_value = mock_domain
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config, max_wait_time=0.1, check_interval=0.01)
        cleaner = TagCleaner(
            conn=mock_conn,
            ssh_checker=checker,
            vm_tracker=vm_tracker,
            tags_to_remove=["test-tag"],
            broken_tag="broken"
        )

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value=None), \
             patch.object(checker, 'wait_for_ssh') as mock_ssh, \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove_tag, \
             patch('vm_manager.tag_cleaner.add_vm_tag') as mock_add_tag:

            await cleaner._monitor_vm(
                "test-uuid-123",
                "test-vm"
            )

            # SSH should not be called (never got an IP)
            mock_ssh.assert_not_called()
            # Tag should not be removed
            mock_remove_tag.assert_not_called()
            # VM should be marked broken
            mock_add_tag.assert_called_once_with(
                mock_conn, mock_domain, "broken"
            )
    
    @pytest.mark.asyncio
    async def test_monitor_vm_ssh_failure(self, tag_cleaner, mock_domain, mock_conn):
        """Test VM monitoring stops if SSH fails (auth_failure)."""
        mock_conn.lookupByName.return_value = mock_domain
        
        async def mock_ssh_failure(*args, **kwargs):
            return "auth_failure"
        
        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="192.168.1.100"), \
             patch.object(tag_cleaner.ssh_checker, 'wait_for_ssh', side_effect=mock_ssh_failure), \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove_tag:
            
            await tag_cleaner._monitor_vm(
                "test-uuid-123",
                "test-vm"
            )
            
            # Tag should not be removed
            mock_remove_tag.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_handle_vm_started_success(self, tag_cleaner, mock_domain):
        """Test handling VM start event successfully."""
        async def mock_monitor(*args):
            pass
        
        with patch.object(tag_cleaner, '_monitor_vm', side_effect=mock_monitor):
            await tag_cleaner.handle_vm_started(mock_domain)
            
            # Wait a bit for task to be registered
            await asyncio.sleep(0.1)
            
            # VM should be tracked
            assert await tag_cleaner.vm_tracker.is_monitoring("test-uuid-123") is True
    
    @pytest.mark.asyncio
    async def test_handle_vm_started_debouncing(self, tag_cleaner, mock_domain, vm_tracker):
        """Test handling duplicate VM start events (debouncing)."""
        # Start monitoring first time
        task1 = asyncio.create_task(asyncio.sleep(10))
        started = await tag_cleaner.vm_tracker.start_monitoring("test-uuid-123", "test-vm", task1)
        assert started is True
        
        # Try to handle start event again - should be debounced
        monitor_call_count = 0
        
        async def mock_monitor(*args):
            nonlocal monitor_call_count
            monitor_call_count += 1
        
        with patch.object(tag_cleaner, '_monitor_vm', side_effect=mock_monitor):
            await tag_cleaner.handle_vm_started(mock_domain)
            
            # Wait for any async operations
            await asyncio.sleep(0.1)
            
            # _monitor_vm should not be called (debounced)
            assert monitor_call_count == 0
        
        # Cleanup
        task1.cancel()
        await tag_cleaner.vm_tracker.stop_monitoring("test-uuid-123")
    
    @pytest.mark.asyncio
    async def test_remove_multiple_tags(self, mock_conn, ssh_checker, vm_tracker, mock_domain):
        """Test removing multiple tags from a VM."""
        mock_conn.lookupByName.return_value = mock_domain
        
        cleaner = TagCleaner(
            conn=mock_conn,
            ssh_checker=ssh_checker,
            vm_tracker=vm_tracker,
            tags_to_remove=["tag1", "tag2", "tag3"]
        )
        
        with patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove_tag:
            await cleaner._remove_tags("test-vm", "test-uuid-123")
            
            # All 3 tags should be removed
            assert mock_remove_tag.call_count == 3
            
            # Verify domain was looked up
            assert mock_conn.lookupByName.call_count == 3
            
            # Verify each tag was removed
            calls = [call[0][2] for call in mock_remove_tag.call_args_list]
            assert "tag1" in calls
            assert "tag2" in calls
            assert "tag3" in calls
    
    @pytest.mark.asyncio
    async def test_remove_tags_handles_errors(self, tag_cleaner, mock_domain, mock_conn):
        """Test that tag removal continues even if one tag fails."""
        mock_conn.lookupByName.return_value = mock_domain
        
        cleaner = TagCleaner(
            conn=mock_conn,
            ssh_checker=tag_cleaner.ssh_checker,
            vm_tracker=tag_cleaner.vm_tracker,
            tags_to_remove=["tag1", "tag2"]
        )
        
        call_count = 0
        
        def mock_remove(conn, domain, tag):
            nonlocal call_count
            call_count += 1
            if tag == "tag1":
                raise Exception("Failed to remove tag1")
            # tag2 should succeed
        
        with patch('vm_manager.tag_cleaner.remove_vm_tag', side_effect=mock_remove):
            await cleaner._remove_tags("test-vm", "test-uuid-123")
            
            # Both tags should be attempted
            assert call_count == 2

    # ------------------------------------------------------------------ #
    # Race condition #1: SSH timeout -> broken tag
    # ------------------------------------------------------------------ #

    @pytest.fixture
    def tag_cleaner_with_broken_tag(self, mock_conn, ssh_checker, vm_tracker):
        """Create a TagCleaner instance with broken_tag configured."""
        return TagCleaner(
            conn=mock_conn,
            ssh_checker=ssh_checker,
            vm_tracker=vm_tracker,
            tags_to_remove=["test-tag"],
            broken_tag="broken"
        )

    @pytest.mark.asyncio
    async def test_monitor_vm_ssh_timeout_calls_mark_broken(
        self, tag_cleaner_with_broken_tag, mock_domain, mock_conn
    ):
        """When SSH returns 'timeout', _monitor_vm marks the VM as broken."""
        cleaner = tag_cleaner_with_broken_tag
        mock_conn.lookupByName.return_value = mock_domain

        async def mock_ssh_timeout(*args, **kwargs):
            return "timeout"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="192.168.1.100"), \
             patch.object(cleaner.ssh_checker, 'wait_for_ssh', side_effect=mock_ssh_timeout), \
             patch('vm_manager.tag_cleaner.add_vm_tag') as mock_add_tag, \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove_tag:

            await cleaner._monitor_vm("test-uuid-123", "test-vm")

            # Broken tag should be added
            mock_add_tag.assert_called_once_with(
                mock_conn, mock_domain, "broken"
            )
            # Original tags should NOT be removed
            mock_remove_tag.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_vm_ssh_timeout_no_broken_tag_configured(
        self, tag_cleaner, mock_domain, mock_conn
    ):
        """When broken_tag is None, SSH timeout should NOT add any tag."""
        mock_conn.lookupByName.return_value = mock_domain

        async def mock_ssh_timeout(*args, **kwargs):
            return "timeout"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="192.168.1.100"), \
             patch.object(tag_cleaner.ssh_checker, 'wait_for_ssh', side_effect=mock_ssh_timeout), \
             patch('vm_manager.tag_cleaner.add_vm_tag') as mock_add_tag, \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove_tag:

            await tag_cleaner._monitor_vm("test-uuid-123", "test-vm")

            # No broken tag configured => add_vm_tag must NOT be called
            mock_add_tag.assert_not_called()
            # Original tags should NOT be removed either
            mock_remove_tag.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_vm_ssh_timeout_adds_broken_tag_string(
        self, mock_conn, ssh_checker, vm_tracker, mock_domain
    ):
        """Explicitly verify add_vm_tag is called with the 'broken' string."""
        mock_conn.lookupByName.return_value = mock_domain
        cleaner = TagCleaner(
            conn=mock_conn,
            ssh_checker=ssh_checker,
            vm_tracker=vm_tracker,
            tags_to_remove=["test-tag"],
            broken_tag="broken"
        )

        async def mock_ssh_timeout(*args, **kwargs):
            return "timeout"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="192.168.1.100"), \
             patch.object(cleaner.ssh_checker, 'wait_for_ssh', side_effect=mock_ssh_timeout), \
             patch('vm_manager.tag_cleaner.add_vm_tag') as mock_add_tag, \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove_tag:

            await cleaner._monitor_vm("test-uuid-123", "test-vm")

            mock_add_tag.assert_called_once()
            actual_tag = mock_add_tag.call_args[0][2]
            assert actual_tag == "broken"
            mock_remove_tag.assert_not_called()

    # ------------------------------------------------------------------ #
    # Race condition #3: 5-second delay before tag removal
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_monitor_vm_sleeps_before_tag_removal(
        self, tag_cleaner, mock_domain, mock_conn
    ):
        """After SSH success, _monitor_vm sleeps 5 seconds before removing tags."""
        mock_conn.lookupByName.return_value = mock_domain

        async def mock_ssh_success(*args, **kwargs):
            return "success"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="192.168.1.100"), \
             patch.object(tag_cleaner.ssh_checker, 'wait_for_ssh', side_effect=mock_ssh_success), \
             patch('vm_manager.tag_cleaner.remove_vm_tag'), \
             patch('vm_manager.tag_cleaner.MetadataManager') as mock_mm_cls, \
             patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:

            mock_mm_cls.return_value.is_in_use.return_value = False

            await tag_cleaner._monitor_vm("test-uuid-123", "test-vm")

            # asyncio.sleep(5) should be called for the pre-removal delay
            mock_sleep.assert_any_call(5)

    # ------------------------------------------------------------------ #
    # Race condition #7: In-use check before tag removal (THE CRITICAL ONE)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_monitor_vm_skips_removal_when_vm_in_use(
        self, tag_cleaner, mock_domain, mock_conn
    ):
        """If _is_vm_in_use returns True, tags are NOT removed."""
        mock_conn.lookupByName.return_value = mock_domain

        async def mock_ssh_success(*args, **kwargs):
            return "success"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="192.168.1.100"), \
             patch.object(tag_cleaner.ssh_checker, 'wait_for_ssh', side_effect=mock_ssh_success), \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove_tag, \
             patch.object(tag_cleaner, '_is_vm_in_use', return_value=True), \
             patch('asyncio.sleep', new_callable=AsyncMock):

            await tag_cleaner._monitor_vm("test-uuid-123", "test-vm")

            # Deployer still active => tags must NOT be removed
            mock_remove_tag.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_vm_removes_tags_when_vm_not_in_use(
        self, tag_cleaner, mock_domain, mock_conn
    ):
        """If _is_vm_in_use returns False, tags ARE removed."""
        mock_conn.lookupByName.return_value = mock_domain

        async def mock_ssh_success(*args, **kwargs):
            return "success"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="192.168.1.100"), \
             patch.object(tag_cleaner.ssh_checker, 'wait_for_ssh', side_effect=mock_ssh_success), \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove_tag, \
             patch.object(tag_cleaner, '_is_vm_in_use', return_value=False), \
             patch('asyncio.sleep', new_callable=AsyncMock):

            await tag_cleaner._monitor_vm("test-uuid-123", "test-vm")

            mock_remove_tag.assert_called_once_with(
                mock_conn, mock_domain, "test-tag"
            )

    # ------------------------------------------------------------------ #
    # _is_vm_in_use method tests
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_is_vm_in_use_returns_true(self, tag_cleaner, mock_domain, mock_conn):
        """Returns True when MetadataManager.is_in_use() returns True."""
        mock_conn.lookupByName.return_value = mock_domain

        with patch('vm_manager.tag_cleaner.MetadataManager') as mock_mm_cls:
            mock_mm_cls.return_value.is_in_use.return_value = True

            result = await tag_cleaner._is_vm_in_use("test-vm")

            assert result is True
            mock_conn.lookupByName.assert_called_with("test-vm")
            mock_mm_cls.assert_called_once_with(mock_domain)
            mock_mm_cls.return_value.is_in_use.assert_called_once()

    @pytest.mark.asyncio
    async def test_is_vm_in_use_returns_false(self, tag_cleaner, mock_domain, mock_conn):
        """Returns False when MetadataManager.is_in_use() returns False."""
        mock_conn.lookupByName.return_value = mock_domain

        with patch('vm_manager.tag_cleaner.MetadataManager') as mock_mm_cls:
            mock_mm_cls.return_value.is_in_use.return_value = False

            result = await tag_cleaner._is_vm_in_use("test-vm")

            assert result is False

    @pytest.mark.asyncio
    async def test_is_vm_in_use_returns_false_on_libvirt_error(
        self, tag_cleaner, mock_conn
    ):
        """Returns False on libvirt error (fail-open to avoid blocking forever)."""
        import libvirt
        mock_conn.lookupByName.side_effect = libvirt.libvirtError("domain not found")

        result = await tag_cleaner._is_vm_in_use("test-vm")

        # Fail-open: return False so tag removal is not permanently blocked
        assert result is False

    # ------------------------------------------------------------------ #
    # _mark_vm_broken method tests
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_mark_vm_broken_adds_tag(
        self, tag_cleaner_with_broken_tag, mock_domain, mock_conn
    ):
        """Adds the broken tag via add_vm_tag when broken_tag is set."""
        cleaner = tag_cleaner_with_broken_tag
        mock_conn.lookupByName.return_value = mock_domain

        with patch('vm_manager.tag_cleaner.add_vm_tag') as mock_add_tag:
            await cleaner._mark_vm_broken("test-vm", "test-uuid-123")

            mock_add_tag.assert_called_once_with(
                mock_conn, mock_domain, "broken"
            )

    @pytest.mark.asyncio
    async def test_mark_vm_broken_noop_when_no_broken_tag(
        self, tag_cleaner, mock_conn
    ):
        """Does nothing when broken_tag is None."""
        with patch('vm_manager.tag_cleaner.add_vm_tag') as mock_add_tag:
            await tag_cleaner._mark_vm_broken("test-vm", "test-uuid-123")

            mock_add_tag.assert_not_called()
            # lookupByName should not even be called when there's no broken_tag
            mock_conn.lookupByName.assert_not_called()

    @pytest.mark.asyncio
    async def test_mark_vm_broken_handles_errors(
        self, tag_cleaner_with_broken_tag, mock_domain, mock_conn
    ):
        """Handles errors gracefully without raising."""
        cleaner = tag_cleaner_with_broken_tag
        mock_conn.lookupByName.return_value = mock_domain

        with patch('vm_manager.tag_cleaner.add_vm_tag',
                   side_effect=Exception("libvirt exploded")):
            # Should not raise
            await cleaner._mark_vm_broken("test-vm", "test-uuid-123")

    # ------------------------------------------------------------------ #
    # Race condition #6: Loopback IP filtering
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_get_vm_ip_retries_when_no_ip(
        self, tag_cleaner, mock_domain, mock_conn
    ):
        """_get_vm_ip_with_retry should retry when get_vm_ip returns None.

        Loopback addresses are filtered by the shared get_vm_ip(), so the
        tag cleaner sees None and retries until a real IP appears.
        """
        mock_conn.lookupByName.return_value = mock_domain
        call_count = 0

        def mock_get_ip(domain, network):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return None  # simulates loopback filtered by get_vm_ip
            return "192.168.1.100"

        with patch('vm_manager.tag_cleaner.get_vm_ip', side_effect=mock_get_ip):
            ip = await tag_cleaner._get_vm_ip_with_retry(
                "test-vm",
                max_attempts=5,
                retry_interval=0.01
            )

            assert ip == "192.168.1.100"
            assert call_count == 3

    @pytest.mark.asyncio
    async def test_get_vm_ip_all_none_returns_none(
        self, tag_cleaner, mock_domain, mock_conn
    ):
        """If all attempts return None (e.g. only loopback), result is None."""
        mock_conn.lookupByName.return_value = mock_domain

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value=None):
            ip = await tag_cleaner._get_vm_ip_with_retry(
                "test-vm",
                max_attempts=3,
                retry_interval=0.01
            )

            assert ip is None

    # ------------------------------------------------------------------ #
    # Monitor VM cancellation
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_monitor_vm_cancellation_stops_tracking(
        self, tag_cleaner, mock_domain, mock_conn, vm_tracker
    ):
        """_monitor_vm handles CancelledError and always calls stop_monitoring."""
        mock_conn.lookupByName.return_value = mock_domain

        async def mock_ssh_hang(*args, **kwargs):
            # Simulate a long-running SSH check that gets cancelled
            await asyncio.sleep(100)
            return "success"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="192.168.1.100"), \
             patch.object(tag_cleaner.ssh_checker, 'wait_for_ssh', side_effect=mock_ssh_hang), \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove_tag, \
             patch.object(vm_tracker, 'stop_monitoring', new_callable=AsyncMock) as mock_stop:

            task = asyncio.create_task(
                tag_cleaner._monitor_vm("test-uuid-123", "test-vm")
            )
            # Let the task start and reach the SSH wait
            await asyncio.sleep(0.05)

            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

            # stop_monitoring must be called in the finally block
            mock_stop.assert_called_once_with("test-uuid-123")
            # Tags must NOT be removed
            mock_remove_tag.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitor_vm_always_stops_tracking_on_success(
        self, tag_cleaner, mock_domain, mock_conn, vm_tracker
    ):
        """stop_monitoring is called even on a successful path."""
        mock_conn.lookupByName.return_value = mock_domain

        async def mock_ssh_success(*args, **kwargs):
            return "success"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="192.168.1.100"), \
             patch.object(tag_cleaner.ssh_checker, 'wait_for_ssh', side_effect=mock_ssh_success), \
             patch('vm_manager.tag_cleaner.remove_vm_tag'), \
             patch.object(tag_cleaner, '_is_vm_in_use', return_value=False), \
             patch('asyncio.sleep', new_callable=AsyncMock), \
             patch.object(vm_tracker, 'stop_monitoring', new_callable=AsyncMock) as mock_stop:

            await tag_cleaner._monitor_vm("test-uuid-123", "test-vm")

            mock_stop.assert_called_once_with("test-uuid-123")

    @pytest.mark.asyncio
    async def test_monitor_vm_always_stops_tracking_on_error(
        self, tag_cleaner, mock_domain, mock_conn, vm_tracker
    ):
        """stop_monitoring is called even when _monitor_vm hits an unexpected error."""
        mock_conn.lookupByName.return_value = mock_domain

        # Patch _get_vm_ip_with_retry itself (not get_vm_ip) so the error
        # escapes into the outer try/except in _monitor_vm, exercising
        # the finally block's stop_monitoring call.
        with patch.object(tag_cleaner, '_get_vm_ip_with_retry',
                          side_effect=RuntimeError("unexpected")), \
             patch.object(vm_tracker, 'stop_monitoring', new_callable=AsyncMock) as mock_stop:

            await tag_cleaner._monitor_vm("test-uuid-123", "test-vm")

            mock_stop.assert_called_once_with("test-uuid-123")


class TestTagCleanerRaceConditions:
    """
    Dedicated tests for race conditions found in production.

    These scenarios involve interleaving of deployer resets, VM reboots,
    and tag-cleaner monitoring that can lead to double-allocation or
    premature tag removal.
    """

    @pytest.fixture
    def mock_conn(self):
        return Mock()

    @pytest.fixture
    def mock_domain(self):
        domain = Mock()
        domain.name.return_value = "test-vm"
        domain.UUIDString.return_value = "test-uuid-123"
        return domain

    @pytest.fixture
    def ssh_checker(self):
        config = SSHConfig(username="root", key_path="/test/key")
        return SSHChecker(config)

    @pytest.fixture
    def vm_tracker(self):
        return VMTracker()

    @pytest.fixture
    def cleaner(self, mock_conn, ssh_checker, vm_tracker):
        return TagCleaner(
            conn=mock_conn,
            ssh_checker=ssh_checker,
            vm_tracker=vm_tracker,
            tags_to_remove=["used", "deploying"]
        )

    @pytest.fixture
    def broken_cleaner(self, mock_conn, ssh_checker, vm_tracker):
        return TagCleaner(
            conn=mock_conn,
            ssh_checker=ssh_checker,
            vm_tracker=vm_tracker,
            tags_to_remove=["used", "deploying"],
            broken_tag="broken"
        )

    # ------------------------------------------------------------------ #
    # Scenario: Mid-playbook reboot (deployer is still active)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_mid_playbook_reboot_does_not_remove_tags(
        self, broken_cleaner, mock_domain, mock_conn
    ):
        """
        A playbook reboots the VM mid-run. SSH comes back up, but
        the deployer is still orchestrating. Tags must NOT be removed.
        """
        mock_conn.lookupByName.return_value = mock_domain

        async def mock_ssh_success(*args, **kwargs):
            return "success"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="10.0.0.5"), \
             patch.object(broken_cleaner.ssh_checker, 'wait_for_ssh',
                         side_effect=mock_ssh_success), \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove, \
             patch('vm_manager.tag_cleaner.add_vm_tag') as mock_add, \
             patch.object(broken_cleaner, '_is_vm_in_use', return_value=True), \
             patch('asyncio.sleep', new_callable=AsyncMock):

            await broken_cleaner._monitor_vm("test-uuid-123", "test-vm")

            mock_remove.assert_not_called()
            mock_add.assert_not_called()

    # ------------------------------------------------------------------ #
    # Scenario: Normal reset cycle (deployer finished, tags should go)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_normal_reset_removes_all_tags(
        self, cleaner, mock_domain, mock_conn
    ):
        """
        Deployer finished, VM rebooted, SSH up, not in use anymore.
        All configured tags should be removed.
        """
        mock_conn.lookupByName.return_value = mock_domain

        async def mock_ssh_success(*args, **kwargs):
            return "success"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="10.0.0.5"), \
             patch.object(cleaner.ssh_checker, 'wait_for_ssh',
                         side_effect=mock_ssh_success), \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove, \
             patch.object(cleaner, '_is_vm_in_use', return_value=False), \
             patch('asyncio.sleep', new_callable=AsyncMock):

            await cleaner._monitor_vm("test-uuid-123", "test-vm")

            assert mock_remove.call_count == 2
            removed_tags = [call[0][2] for call in mock_remove.call_args_list]
            assert "used" in removed_tags
            assert "deploying" in removed_tags

    # ------------------------------------------------------------------ #
    # Scenario: SSH timeout on broken VM
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_broken_vm_gets_broken_tag_keeps_used_tag(
        self, broken_cleaner, mock_domain, mock_conn
    ):
        """
        VM SSH times out (kernel panic, disk full, etc.).
        'broken' tag is added but 'used' tag is intentionally kept
        so no other deployer picks it up.
        """
        mock_conn.lookupByName.return_value = mock_domain

        async def mock_ssh_timeout(*args, **kwargs):
            return "timeout"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="10.0.0.5"), \
             patch.object(broken_cleaner.ssh_checker, 'wait_for_ssh',
                         side_effect=mock_ssh_timeout), \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove, \
             patch('vm_manager.tag_cleaner.add_vm_tag') as mock_add:

            await broken_cleaner._monitor_vm("test-uuid-123", "test-vm")

            # 'broken' tag added
            mock_add.assert_called_once_with(mock_conn, mock_domain, "broken")
            # 'used' and 'deploying' tags NOT removed
            mock_remove.assert_not_called()

    # ------------------------------------------------------------------ #
    # Scenario: Rapid consecutive reboots (debounce + race)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_rapid_reboots_only_one_monitor_runs(
        self, cleaner, mock_domain, mock_conn, vm_tracker
    ):
        """
        Two VM start events fire in quick succession. Only the first
        should be monitored; the second should be debounced.
        """
        monitor_calls = 0

        async def mock_monitor(*args):
            nonlocal monitor_calls
            monitor_calls += 1
            await asyncio.sleep(0.1)

        with patch.object(cleaner, '_monitor_vm', side_effect=mock_monitor):
            await cleaner.handle_vm_started(mock_domain)
            await cleaner.handle_vm_started(mock_domain)

            await asyncio.sleep(0.2)

            assert monitor_calls == 1

    # ------------------------------------------------------------------ #
    # Scenario: In-use check timing (5s window matters)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_five_second_delay_happens_before_in_use_check(
        self, cleaner, mock_domain, mock_conn
    ):
        """
        Verify the order: SSH success -> sleep(5) -> _is_vm_in_use -> remove.
        The 5s delay gives the deployer time to update metadata.
        """
        mock_conn.lookupByName.return_value = mock_domain
        call_order = []

        async def mock_ssh_success(*args, **kwargs):
            return "success"

        original_is_in_use = cleaner._is_vm_in_use

        async def tracking_is_in_use(vm_name):
            call_order.append("is_in_use")
            return False

        async def tracking_sleep(seconds):
            call_order.append(f"sleep({seconds})")

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="10.0.0.5"), \
             patch.object(cleaner.ssh_checker, 'wait_for_ssh',
                         side_effect=mock_ssh_success), \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove, \
             patch.object(cleaner, '_is_vm_in_use', side_effect=tracking_is_in_use), \
             patch('asyncio.sleep', side_effect=tracking_sleep):

            await cleaner._monitor_vm("test-uuid-123", "test-vm")

            # sleep(5) must happen before is_in_use check
            assert "sleep(5)" in call_order
            assert "is_in_use" in call_order
            sleep_idx = call_order.index("sleep(5)")
            in_use_idx = call_order.index("is_in_use")
            assert sleep_idx < in_use_idx, (
                f"sleep(5) at index {sleep_idx} should come before "
                f"is_in_use at index {in_use_idx}"
            )
            # Tags should be removed after both checks pass
            assert mock_remove.call_count == 2

    # ------------------------------------------------------------------ #
    # Scenario: Loopback during DHCP renewal (transient 127.x.x.x)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_no_ip_during_dhcp_renewal_retries_until_real_ip(
        self, cleaner, mock_domain, mock_conn
    ):
        """
        During DHCP renewal, get_vm_ip returns None (loopback addresses are
        filtered by the shared get_vm_ip). The tag cleaner must keep retrying
        until a real IP appears.
        """
        mock_conn.lookupByName.return_value = mock_domain
        ips = [None, None, None, "10.0.0.5"]
        call_idx = 0

        def rotating_ip(domain, network):
            nonlocal call_idx
            ip = ips[call_idx] if call_idx < len(ips) else ips[-1]
            call_idx += 1
            return ip

        with patch('vm_manager.tag_cleaner.get_vm_ip', side_effect=rotating_ip):
            ip = await cleaner._get_vm_ip_with_retry(
                "test-vm",
                max_attempts=10,
                retry_interval=0.01
            )

            assert ip == "10.0.0.5"
            # Loopback IPs (first 2) + None (1) + valid (1) = 4 calls
            assert call_idx == 4

    # ------------------------------------------------------------------ #
    # Scenario: auth_failure result does not mark broken or remove tags
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_ssh_auth_failure_no_broken_tag_no_removal(
        self, broken_cleaner, mock_domain, mock_conn
    ):
        """
        SSH auth_failure is neither 'timeout' nor 'success'.
        The VM should not be marked broken and tags should not be removed.
        """
        mock_conn.lookupByName.return_value = mock_domain

        async def mock_ssh_auth_failure(*args, **kwargs):
            return "auth_failure"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="10.0.0.5"), \
             patch.object(broken_cleaner.ssh_checker, 'wait_for_ssh',
                         side_effect=mock_ssh_auth_failure), \
             patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove, \
             patch('vm_manager.tag_cleaner.add_vm_tag') as mock_add:

            await broken_cleaner._monitor_vm("test-uuid-123", "test-vm")

            # auth_failure != "timeout" => no broken tag
            mock_add.assert_not_called()
            # auth_failure != "success" => no tag removal
            mock_remove.assert_not_called()


class TestOnBrokenScript:
    """Tests for the --on-broken external script execution."""

    @pytest.fixture
    def mock_conn(self):
        return Mock()

    @pytest.fixture
    def mock_domain(self):
        domain = Mock()
        domain.name.return_value = "test-vm"
        domain.UUIDString.return_value = "test-uuid-123"
        return domain

    @pytest.fixture
    def ssh_checker(self):
        config = SSHConfig(username="root", key_path="/test/key")
        return SSHChecker(config, max_wait_time=1800)

    @pytest.fixture
    def vm_tracker(self):
        return VMTracker()

    @pytest.fixture
    def cleaner_with_script(self, mock_conn, ssh_checker, vm_tracker):
        """TagCleaner with on_broken script configured."""
        return TagCleaner(
            conn=mock_conn,
            ssh_checker=ssh_checker,
            vm_tracker=vm_tracker,
            tags_to_remove=["used"],
            broken_tag="broken",
            on_broken="/path/to/handler.sh",
            libvirt_uri="qemu:///system"
        )

    @pytest.fixture
    def cleaner_without_script(self, mock_conn, ssh_checker, vm_tracker):
        """TagCleaner without on_broken script."""
        return TagCleaner(
            conn=mock_conn,
            ssh_checker=ssh_checker,
            vm_tracker=vm_tracker,
            tags_to_remove=["used"],
            broken_tag="broken",
            on_broken=None,
        )

    @pytest.mark.asyncio
    async def test_stores_on_broken_and_libvirt_uri(self, cleaner_with_script):
        """Constructor stores on_broken, libvirt_uri, and on-broken options."""
        assert cleaner_with_script.on_broken == "/path/to/handler.sh"
        assert cleaner_with_script.libvirt_uri == "qemu:///system"
        assert cleaner_with_script.on_broken_timeout == 300
        assert cleaner_with_script.on_broken_retries is None
        assert cleaner_with_script.on_broken_retry_delay == 60

    @pytest.mark.asyncio
    async def test_run_on_broken_script_called_after_mark_broken(
        self, cleaner_with_script, mock_domain, mock_conn
    ):
        """After SSH timeout, both _mark_vm_broken and _run_on_broken_script are called."""
        mock_conn.lookupByName.return_value = mock_domain

        async def mock_ssh_timeout(*args, **kwargs):
            return "timeout"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="10.0.0.5"), \
             patch.object(cleaner_with_script.ssh_checker, 'wait_for_ssh',
                         side_effect=mock_ssh_timeout), \
             patch('vm_manager.tag_cleaner.add_vm_tag'), \
             patch.object(cleaner_with_script, '_run_on_broken_script',
                         new_callable=AsyncMock, return_value=False) as mock_run_script:

            await cleaner_with_script._monitor_vm("test-uuid-123", "test-vm")

            mock_run_script.assert_called_once_with(
                "test-vm", "test-uuid-123", "10.0.0.5"
            )

    @pytest.mark.asyncio
    async def test_run_on_broken_script_not_called_when_disabled(
        self, cleaner_without_script, mock_domain, mock_conn
    ):
        """When on_broken is None, _run_on_broken_script returns immediately."""
        # Should return False without doing anything
        result = await cleaner_without_script._run_on_broken_script(
            "test-vm", "test-uuid-123", "10.0.0.5"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_script_receives_correct_env_vars(
        self, cleaner_with_script, mock_conn
    ):
        """The external script receives VM information as environment variables."""
        mock_conn.lookupByName.return_value = Mock()

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=["linux-test", "used", "broken"]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:

            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await cleaner_with_script._run_on_broken_script(
                "test-vm", "test-uuid-456", "10.0.0.5"
            )

            # Verify the script was called
            mock_exec.assert_called_once()
            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")

            assert env["VM_NAME"] == "test-vm"
            assert env["VM_UUID"] == "test-uuid-456"
            assert env["VM_IP"] == "10.0.0.5"
            assert env["VM_TAGS"] == "linux-test,used,broken"
            assert env["VM_BROKEN_TAG"] == "broken"
            assert env["VM_WAIT_TIME"] == "1800"
            assert env["LIBVIRT_URI"] == "qemu:///system"

    @pytest.mark.asyncio
    async def test_script_nonzero_exit_does_not_raise(
        self, cleaner_with_script, mock_conn
    ):
        """Non-zero exit code from script is logged but does not raise."""
        mock_conn.lookupByName.return_value = Mock()
        # Set retries=0 so it doesn't retry after failure
        cleaner_with_script.on_broken_retries = 0

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:

            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"error output", b"stderr output")
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc

            # Should not raise, and should return False
            result = await cleaner_with_script._run_on_broken_script(
                "test-vm", "test-uuid-123", "10.0.0.5"
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_script_timeout_kills_process(
        self, cleaner_with_script, mock_conn
    ):
        """Script exceeding on_broken_timeout is killed."""
        mock_conn.lookupByName.return_value = Mock()
        # Set retries=0 so it doesn't retry after timeout
        cleaner_with_script.on_broken_retries = 0

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:

            mock_proc = AsyncMock()
            mock_proc.communicate.side_effect = asyncio.TimeoutError()
            mock_proc.kill = Mock()
            mock_proc.wait = AsyncMock()
            mock_exec.return_value = mock_proc

            # Should not raise, and should return False
            result = await cleaner_with_script._run_on_broken_script(
                "test-vm", "test-uuid-123", "10.0.0.5"
            )
            assert result is False
            mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_script_exception_does_not_propagate(
        self, cleaner_with_script, mock_conn
    ):
        """Exceptions from subprocess are caught and logged, not propagated."""
        mock_conn.lookupByName.return_value = Mock()
        # Set retries=0 so it doesn't retry after exception
        cleaner_with_script.on_broken_retries = 0

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec',
                   side_effect=FileNotFoundError("/path/to/handler.sh")):

            # Should not raise, and should return False
            result = await cleaner_with_script._run_on_broken_script(
                "test-vm", "test-uuid-123", "10.0.0.5"
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_empty_ip_passed_when_none(
        self, cleaner_with_script, mock_conn
    ):
        """When IP is None, VM_IP env var is empty string."""
        mock_conn.lookupByName.return_value = Mock()

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:

            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await cleaner_with_script._run_on_broken_script(
                "test-vm", "test-uuid-123", None  # No IP
            )

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env["VM_IP"] == ""

    @pytest.mark.asyncio
    async def test_script_retries_on_failure(
        self, cleaner_with_script, mock_conn
    ):
        """Script is retried on non-zero exit until it succeeds."""
        mock_conn.lookupByName.return_value = Mock()
        cleaner_with_script.on_broken_retry_delay = 0  # no delay in tests

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:

            # Fail twice, then succeed
            mock_proc_fail = AsyncMock()
            mock_proc_fail.communicate.return_value = (b"", b"")
            mock_proc_fail.returncode = 1

            mock_proc_ok = AsyncMock()
            mock_proc_ok.communicate.return_value = (b"", b"")
            mock_proc_ok.returncode = 0

            mock_exec.side_effect = [mock_proc_fail, mock_proc_fail, mock_proc_ok]

            result = await cleaner_with_script._run_on_broken_script(
                "test-vm", "test-uuid-123", "10.0.0.5"
            )

            assert result is True
            assert mock_exec.call_count == 3

    @pytest.mark.asyncio
    async def test_script_retries_limited_by_on_broken_retries(
        self, cleaner_with_script, mock_conn
    ):
        """Script stops retrying after on_broken_retries attempts."""
        mock_conn.lookupByName.return_value = Mock()
        cleaner_with_script.on_broken_retries = 2  # 1 initial + 2 retries = 3 total
        cleaner_with_script.on_broken_retry_delay = 0

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:

            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc

            result = await cleaner_with_script._run_on_broken_script(
                "test-vm", "test-uuid-123", "10.0.0.5"
            )

            assert result is False
            # 1 initial + 2 retries = 3 total
            assert mock_exec.call_count == 3

    @pytest.mark.asyncio
    async def test_script_retries_on_timeout(
        self, cleaner_with_script, mock_conn
    ):
        """Script is retried after timeout, then succeeds."""
        mock_conn.lookupByName.return_value = Mock()
        cleaner_with_script.on_broken_retry_delay = 0

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:

            # First call times out
            mock_proc_timeout = AsyncMock()
            mock_proc_timeout.communicate.side_effect = asyncio.TimeoutError()
            mock_proc_timeout.kill = Mock()
            mock_proc_timeout.wait = AsyncMock()

            # Second call succeeds
            mock_proc_ok = AsyncMock()
            mock_proc_ok.communicate.return_value = (b"", b"")
            mock_proc_ok.returncode = 0

            mock_exec.side_effect = [mock_proc_timeout, mock_proc_ok]

            result = await cleaner_with_script._run_on_broken_script(
                "test-vm", "test-uuid-123", "10.0.0.5"
            )

            assert result is True
            assert mock_exec.call_count == 2
            mock_proc_timeout.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_script_uses_configurable_timeout(
        self, mock_conn, ssh_checker, vm_tracker
    ):
        """Script uses on_broken_timeout instead of hardcoded 60s."""
        cleaner = TagCleaner(
            conn=mock_conn,
            ssh_checker=ssh_checker,
            vm_tracker=vm_tracker,
            tags_to_remove=["used"],
            broken_tag="broken",
            on_broken="/path/to/handler.sh",
            on_broken_timeout=600,
            on_broken_retries=0,
        )
        mock_conn.lookupByName.return_value = Mock()

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:

            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            # Patch asyncio.wait_for to verify the timeout value
            with patch('asyncio.wait_for', new_callable=AsyncMock) as mock_wait_for:
                mock_wait_for.return_value = (b"", b"")

                await cleaner._run_on_broken_script(
                    "test-vm", "test-uuid-123", "10.0.0.5"
                )

                # Verify timeout=600 was passed to wait_for
                mock_wait_for.assert_called_once()
                _, kwargs = mock_wait_for.call_args
                assert kwargs["timeout"] == 600

    @pytest.mark.asyncio
    async def test_script_zero_retries_runs_once(
        self, cleaner_with_script, mock_conn
    ):
        """With on_broken_retries=0, script runs exactly once even on failure."""
        mock_conn.lookupByName.return_value = Mock()
        cleaner_with_script.on_broken_retries = 0

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:

            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc

            await cleaner_with_script._run_on_broken_script(
                "test-vm", "test-uuid-123", "10.0.0.5"
            )

            assert mock_exec.call_count == 1


class TestOnBrokenReturnValues:
    """Tests for _run_on_broken_script returning bool."""

    @pytest.fixture
    def mock_conn(self):
        return Mock()

    @pytest.fixture
    def ssh_checker(self):
        config = SSHConfig(username="root", key_path="/test/key")
        return SSHChecker(config, max_wait_time=1800)

    @pytest.fixture
    def vm_tracker(self):
        return VMTracker()

    @pytest.mark.asyncio
    async def test_returns_false_when_disabled(self, mock_conn, ssh_checker, vm_tracker):
        """When on_broken is None, returns False immediately."""
        cleaner = TagCleaner(
            conn=mock_conn, ssh_checker=ssh_checker, vm_tracker=vm_tracker,
            tags_to_remove=["used"], on_broken=None,
        )
        result = await cleaner._run_on_broken_script("vm", "uuid", "10.0.0.1")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self, mock_conn, ssh_checker, vm_tracker):
        """Returns True when script exits 0."""
        cleaner = TagCleaner(
            conn=mock_conn, ssh_checker=ssh_checker, vm_tracker=vm_tracker,
            tags_to_remove=["used"], broken_tag="broken",
            on_broken="/path/to/handler.sh",
        )
        mock_conn.lookupByName.return_value = Mock()

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            result = await cleaner._run_on_broken_script("vm", "uuid", "10.0.0.1")
            assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_retries_exhausted(self, mock_conn, ssh_checker, vm_tracker):
        """Returns False when all retries fail."""
        cleaner = TagCleaner(
            conn=mock_conn, ssh_checker=ssh_checker, vm_tracker=vm_tracker,
            tags_to_remove=["used"], broken_tag="broken",
            on_broken="/path/to/handler.sh",
            on_broken_retries=1, on_broken_retry_delay=0,
        )
        mock_conn.lookupByName.return_value = Mock()

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc

            result = await cleaner._run_on_broken_script("vm", "uuid", "10.0.0.1")
            assert result is False
            assert mock_exec.call_count == 2  # 1 initial + 1 retry


class TestRepairFlow:
    """Tests for the post-success repair flow (Issue 1 fix)."""

    @pytest.fixture
    def mock_conn(self):
        return Mock()

    @pytest.fixture
    def mock_domain(self):
        domain = Mock()
        domain.name.return_value = "test-vm"
        domain.UUIDString.return_value = "test-uuid-123"
        return domain

    @pytest.fixture
    def ssh_checker(self):
        config = SSHConfig(username="root", key_path="/test/key")
        return SSHChecker(config, max_wait_time=1800)

    @pytest.fixture
    def vm_tracker(self):
        return VMTracker()

    @pytest.mark.asyncio
    async def test_tracker_freed_before_on_broken_script(
        self, mock_conn, ssh_checker, vm_tracker, mock_domain
    ):
        """Tracker slot is freed BEFORE the on-broken script runs,
        so if the script restarts the VM, new events aren't debounced."""
        mock_conn.lookupByName.return_value = mock_domain
        cleaner = TagCleaner(
            conn=mock_conn, ssh_checker=ssh_checker, vm_tracker=vm_tracker,
            tags_to_remove=["used"], broken_tag="broken",
            on_broken="/path/to/handler.sh",
            on_broken_retries=0,  # run once, don't retry
        )

        call_order = []

        original_stop = vm_tracker.stop_monitoring

        async def tracking_stop(uuid):
            call_order.append("stop_monitoring")
            await original_stop(uuid)

        async def tracking_run_script(*args, **kwargs):
            call_order.append("run_on_broken_script")
            return False

        async def mock_ssh_timeout(*args, **kwargs):
            return "timeout"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="10.0.0.5"), \
             patch.object(cleaner.ssh_checker, 'wait_for_ssh',
                         side_effect=mock_ssh_timeout), \
             patch('vm_manager.tag_cleaner.add_vm_tag'), \
             patch.object(vm_tracker, 'stop_monitoring', side_effect=tracking_stop), \
             patch.object(cleaner, '_run_on_broken_script',
                         side_effect=tracking_run_script):

            await cleaner._monitor_vm("test-uuid-123", "test-vm")

        assert "stop_monitoring" in call_order
        assert "run_on_broken_script" in call_order
        stop_idx = call_order.index("stop_monitoring")
        script_idx = call_order.index("run_on_broken_script")
        assert stop_idx < script_idx, (
            "stop_monitoring must be called before _run_on_broken_script"
        )

    @pytest.mark.asyncio
    async def test_broken_tag_removed_after_successful_repair(
        self, mock_conn, ssh_checker, vm_tracker, mock_domain
    ):
        """After on-broken script succeeds, the broken tag is removed."""
        mock_conn.lookupByName.return_value = mock_domain
        cleaner = TagCleaner(
            conn=mock_conn, ssh_checker=ssh_checker, vm_tracker=vm_tracker,
            tags_to_remove=["used"], broken_tag="broken",
        )

        with patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove:
            await cleaner._remove_broken_tag("test-vm", "test-uuid-123")

            mock_remove.assert_called_once_with(
                mock_conn, mock_domain, "broken"
            )

    @pytest.mark.asyncio
    async def test_remove_broken_tag_noop_when_no_broken_tag(
        self, mock_conn, ssh_checker, vm_tracker
    ):
        """_remove_broken_tag does nothing when broken_tag is None."""
        cleaner = TagCleaner(
            conn=mock_conn, ssh_checker=ssh_checker, vm_tracker=vm_tracker,
            tags_to_remove=["used"], broken_tag=None,
        )

        with patch('vm_manager.tag_cleaner.remove_vm_tag') as mock_remove:
            await cleaner._remove_broken_tag("test-vm", "uuid")
            mock_remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_successful_repair_triggers_monitoring(
        self, mock_conn, ssh_checker, vm_tracker, mock_domain
    ):
        """After repair, handle_vm_started is called to start fresh monitoring."""
        mock_conn.lookupByName.return_value = mock_domain
        cleaner = TagCleaner(
            conn=mock_conn, ssh_checker=ssh_checker, vm_tracker=vm_tracker,
            tags_to_remove=["used"], broken_tag="broken",
        )

        with patch('vm_manager.tag_cleaner.remove_vm_tag'), \
             patch.object(cleaner, 'handle_vm_started',
                         new_callable=AsyncMock) as mock_handle:

            await cleaner._handle_successful_repair("test-vm", "test-uuid-123")

            mock_handle.assert_awaited_once_with(mock_domain)

    @pytest.mark.asyncio
    async def test_no_repair_when_script_fails(
        self, mock_conn, ssh_checker, vm_tracker, mock_domain
    ):
        """When on-broken script fails (retries exhausted), no repair actions taken."""
        mock_conn.lookupByName.return_value = mock_domain
        cleaner = TagCleaner(
            conn=mock_conn, ssh_checker=ssh_checker, vm_tracker=vm_tracker,
            tags_to_remove=["used"], broken_tag="broken",
            on_broken="/path/to/handler.sh",
            on_broken_retries=0,
        )

        async def mock_ssh_timeout(*args, **kwargs):
            return "timeout"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="10.0.0.5"), \
             patch.object(cleaner.ssh_checker, 'wait_for_ssh',
                         side_effect=mock_ssh_timeout), \
             patch('vm_manager.tag_cleaner.add_vm_tag'), \
             patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec, \
             patch.object(cleaner, '_handle_successful_repair',
                         new_callable=AsyncMock) as mock_repair:

            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 1
            mock_exec.return_value = mock_proc

            await cleaner._monitor_vm("test-uuid-123", "test-vm")

            # Script failed → no repair
            mock_repair.assert_not_called()

    @pytest.mark.asyncio
    async def test_repair_triggered_when_script_succeeds(
        self, mock_conn, ssh_checker, vm_tracker, mock_domain
    ):
        """When on-broken script succeeds, _handle_successful_repair IS called."""
        mock_conn.lookupByName.return_value = mock_domain
        cleaner = TagCleaner(
            conn=mock_conn, ssh_checker=ssh_checker, vm_tracker=vm_tracker,
            tags_to_remove=["used"], broken_tag="broken",
            on_broken="/path/to/handler.sh",
        )

        async def mock_ssh_timeout(*args, **kwargs):
            return "timeout"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="10.0.0.5"), \
             patch.object(cleaner.ssh_checker, 'wait_for_ssh',
                         side_effect=mock_ssh_timeout), \
             patch('vm_manager.tag_cleaner.add_vm_tag'), \
             patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec, \
             patch.object(cleaner, '_handle_successful_repair',
                         new_callable=AsyncMock) as mock_repair:

            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 0
            mock_exec.return_value = mock_proc

            await cleaner._monitor_vm("test-uuid-123", "test-vm")

            mock_repair.assert_awaited_once_with("test-vm", "test-uuid-123")

    @pytest.mark.asyncio
    async def test_tracker_not_double_freed_on_timeout_path(
        self, mock_conn, ssh_checker, vm_tracker, mock_domain
    ):
        """On timeout path, stop_monitoring is called exactly once
        (before script), not again in the finally block."""
        mock_conn.lookupByName.return_value = mock_domain
        cleaner = TagCleaner(
            conn=mock_conn, ssh_checker=ssh_checker, vm_tracker=vm_tracker,
            tags_to_remove=["used"], broken_tag="broken",
        )

        async def mock_ssh_timeout(*args, **kwargs):
            return "timeout"

        with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="10.0.0.5"), \
             patch.object(cleaner.ssh_checker, 'wait_for_ssh',
                         side_effect=mock_ssh_timeout), \
             patch('vm_manager.tag_cleaner.add_vm_tag'), \
             patch.object(vm_tracker, 'stop_monitoring',
                         new_callable=AsyncMock) as mock_stop:

            await cleaner._monitor_vm("test-uuid-123", "test-vm")

            # Called once (before script), NOT again in finally
            mock_stop.assert_called_once_with("test-uuid-123")


class TestCancelledErrorHandling:
    """Tests for CancelledError propagation and process cleanup."""

    @pytest.fixture
    def mock_conn(self):
        return Mock()

    @pytest.fixture
    def ssh_checker(self):
        config = SSHConfig(username="root", key_path="/test/key")
        return SSHChecker(config, max_wait_time=1800)

    @pytest.fixture
    def vm_tracker(self):
        return VMTracker()

    @pytest.mark.asyncio
    async def test_cancelled_during_script_kills_process(
        self, mock_conn, ssh_checker, vm_tracker
    ):
        """CancelledError during script execution kills the child process."""
        cleaner = TagCleaner(
            conn=mock_conn, ssh_checker=ssh_checker, vm_tracker=vm_tracker,
            tags_to_remove=["used"], broken_tag="broken",
            on_broken="/path/to/handler.sh",
            on_broken_timeout=300,
        )

        import os
        env = os.environ.copy()

        with patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec:
            mock_proc = AsyncMock()
            # communicate() raises CancelledError (simulating task cancellation)
            mock_proc.communicate.side_effect = asyncio.CancelledError()
            mock_proc.returncode = None  # process still running
            mock_proc.kill = Mock()
            mock_proc.wait = AsyncMock()
            mock_exec.return_value = mock_proc

            with pytest.raises(asyncio.CancelledError):
                await cleaner._execute_on_broken_script("test-vm", env)

            # Process must be killed
            mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancelled_during_retry_sleep_propagates(
        self, mock_conn, ssh_checker, vm_tracker
    ):
        """CancelledError during retry sleep propagates correctly."""
        cleaner = TagCleaner(
            conn=mock_conn, ssh_checker=ssh_checker, vm_tracker=vm_tracker,
            tags_to_remove=["used"], broken_tag="broken",
            on_broken="/path/to/handler.sh",
            on_broken_retry_delay=60,
        )
        mock_conn.lookupByName.return_value = Mock()

        call_count = 0

        async def mock_sleep(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 1 and seconds == 60:
                # Simulate cancellation during retry delay
                raise asyncio.CancelledError()

        with patch('vm_manager.tag_cleaner.get_vm_tags', return_value=[]), \
             patch('asyncio.create_subprocess_exec', new_callable=AsyncMock) as mock_exec, \
             patch('asyncio.sleep', side_effect=mock_sleep):

            mock_proc = AsyncMock()
            mock_proc.communicate.return_value = (b"", b"")
            mock_proc.returncode = 1  # fail, triggering retry
            mock_exec.return_value = mock_proc

            with pytest.raises(asyncio.CancelledError):
                await cleaner._run_on_broken_script(
                    "test-vm", "test-uuid-123", "10.0.0.5"
                )
