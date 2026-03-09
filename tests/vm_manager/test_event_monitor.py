"""
Unit tests for EventMonitor (libvirt event monitoring).
"""
import pytest
import asyncio
from unittest.mock import Mock, patch, MagicMock, call
import libvirt
from vm_manager.event_monitor import EventMonitor


class TestEventMonitor:
    """Test libvirt event monitoring."""
    
    @pytest.fixture
    def mock_conn(self):
        """Create a mock libvirt connection.
        
        Returns two callback IDs since start() now registers both
        lifecycle (42) and reboot (43) callbacks.
        """
        conn = Mock()
        conn.domainEventRegisterAny.side_effect = [42, 43]
        return conn
    
    @pytest.fixture
    def on_vm_started(self):
        """Create a mock VM started callback."""
        return Mock()
    
    @pytest.fixture
    def on_vm_stopped(self):
        """Create a mock VM stopped callback."""
        return Mock()
    
    def test_event_monitor_creation(self, mock_conn, on_vm_started):
        """Test creating an EventMonitor."""
        monitor = EventMonitor(
            conn=mock_conn,
            on_vm_started=on_vm_started
        )
        
        assert monitor.conn == mock_conn
        assert monitor.on_vm_started == on_vm_started
        assert monitor.on_vm_stopped is None
        assert monitor._running is False
    
    def test_event_monitor_with_stop_callback(self, mock_conn, on_vm_started, on_vm_stopped):
        """Test creating EventMonitor with stop callback."""
        monitor = EventMonitor(
            conn=mock_conn,
            on_vm_started=on_vm_started,
            on_vm_stopped=on_vm_stopped
        )
        
        assert monitor.on_vm_stopped == on_vm_stopped
    
    @pytest.mark.asyncio
    async def test_start_registers_callback(self, mock_conn, on_vm_started):
        """Test starting the monitor registers event callback."""
        # Mock virEventRunDefaultImpl to prevent blocking
        with patch('libvirt.virEventRunDefaultImpl'):
            monitor = EventMonitor(
                conn=mock_conn,
                on_vm_started=on_vm_started
            )
            
            await monitor.start()
            
            # Verify lifecycle callback was registered (first call)
            first_call = mock_conn.domainEventRegisterAny.call_args_list[0]
            assert first_call == call(
                None,  # All domains
                libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE,
                monitor._lifecycle_callback,
                None  # opaque
            )
            
            assert monitor._running is True
            assert 42 in monitor._callback_ids
            
            # Cleanup
            await monitor.stop()
    
    @pytest.mark.asyncio
    async def test_stop_deregisters_callback(self, mock_conn, on_vm_started):
        """Test stopping the monitor deregisters callbacks."""
        with patch('libvirt.virEventRunDefaultImpl'):
            monitor = EventMonitor(
                conn=mock_conn,
                on_vm_started=on_vm_started
            )
            
            await monitor.start()
            await monitor.stop()
            
            # Verify callback was deregistered (callback ID 42)
            mock_conn.domainEventDeregisterAny.assert_any_call(42)
            assert monitor._running is False
            assert len(monitor._callback_ids) == 0
    
    def test_lifecycle_callback_started_event(self, mock_conn, on_vm_started):
        """Test lifecycle callback handles VIR_DOMAIN_EVENT_STARTED."""
        monitor = EventMonitor(
            conn=mock_conn,
            on_vm_started=on_vm_started
        )
        
        mock_domain = Mock()
        mock_domain.name.return_value = "test-vm"
        
        # Call the callback with STARTED event
        monitor._lifecycle_callback(
            mock_conn,
            mock_domain,
            libvirt.VIR_DOMAIN_EVENT_STARTED,  # event
            0,  # detail
            None  # opaque
        )
        
        # Verify on_vm_started was called
        on_vm_started.assert_called_once_with(mock_domain)
    
    def test_lifecycle_callback_stopped_event(self, mock_conn, on_vm_started, on_vm_stopped):
        """Test lifecycle callback handles VIR_DOMAIN_EVENT_STOPPED."""
        monitor = EventMonitor(
            conn=mock_conn,
            on_vm_started=on_vm_started,
            on_vm_stopped=on_vm_stopped
        )
        
        mock_domain = Mock()
        mock_domain.name.return_value = "test-vm"
        
        # Call the callback with STOPPED event
        monitor._lifecycle_callback(
            mock_conn,
            mock_domain,
            libvirt.VIR_DOMAIN_EVENT_STOPPED,  # event
            0,  # detail
            None  # opaque
        )
        
        # Verify on_vm_stopped was called
        on_vm_stopped.assert_called_once_with(mock_domain)
        # on_vm_started should not be called
        on_vm_started.assert_not_called()
    
    def test_lifecycle_callback_stopped_no_callback(self, mock_conn, on_vm_started):
        """Test STOPPED event when no stop callback registered."""
        monitor = EventMonitor(
            conn=mock_conn,
            on_vm_started=on_vm_started,
            on_vm_stopped=None
        )
        
        mock_domain = Mock()
        mock_domain.name.return_value = "test-vm"
        
        # Should not raise error even though no callback
        monitor._lifecycle_callback(
            mock_conn,
            mock_domain,
            libvirt.VIR_DOMAIN_EVENT_STOPPED,
            0,
            None
        )
        
        # on_vm_started should not be called
        on_vm_started.assert_not_called()
    
    def test_lifecycle_callback_handles_exceptions(self, mock_conn, on_vm_started):
        """Test lifecycle callback handles exceptions in user callback."""
        def failing_callback(domain):
            raise Exception("User callback failed")
        
        monitor = EventMonitor(
            conn=mock_conn,
            on_vm_started=failing_callback
        )
        
        mock_domain = Mock()
        mock_domain.name.return_value = "test-vm"
        
        # Should not raise - exception should be caught
        monitor._lifecycle_callback(
            mock_conn,
            mock_domain,
            libvirt.VIR_DOMAIN_EVENT_STARTED,
            0,
            None
        )
    
    def test_lifecycle_callback_unknown_event(self, mock_conn, on_vm_started):
        """Test lifecycle callback ignores unknown events."""
        monitor = EventMonitor(
            conn=mock_conn,
            on_vm_started=on_vm_started
        )
        
        mock_domain = Mock()
        
        # Call with unknown event
        monitor._lifecycle_callback(
            mock_conn,
            mock_domain,
            999,  # unknown event
            0,
            None
        )
        
        # Callback should not be called
        on_vm_started.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_start_already_running(self, mock_conn, on_vm_started):
        """Test starting when already running doesn't re-register."""
        with patch('libvirt.virEventRunDefaultImpl'):
            monitor = EventMonitor(
                conn=mock_conn,
                on_vm_started=on_vm_started
            )
            
            await monitor.start()
            
            # Start again
            await monitor.start()
            
            # Should only register callbacks once (2 callbacks: lifecycle + reboot)
            assert mock_conn.domainEventRegisterAny.call_count == 2
            
            # Cleanup
            await monitor.stop()
    
    @pytest.mark.asyncio
    async def test_stop_not_running(self, mock_conn, on_vm_started):
        """Test stopping when not running is safe."""
        monitor = EventMonitor(
            conn=mock_conn,
            on_vm_started=on_vm_started
        )
        
        # Stop without starting
        await monitor.stop()
        
        # Should not call deregister
        mock_conn.domainEventDeregisterAny.assert_not_called()


class TestEventMonitorRebootCallback:
    """Tests for reboot callback registration and handling (Race condition #1 fix)."""

    @pytest.fixture
    def mock_conn(self):
        """Create a mock libvirt connection returning two callback IDs."""
        conn = Mock()
        conn.domainEventRegisterAny.side_effect = [42, 43]  # lifecycle, reboot
        return conn

    @pytest.fixture
    def on_vm_started(self):
        """Create a mock VM started callback."""
        return Mock()

    @pytest.fixture
    def on_vm_stopped(self):
        """Create a mock VM stopped callback."""
        return Mock()

    @pytest.mark.asyncio
    async def test_start_registers_lifecycle_and_reboot_callbacks(self, mock_conn, on_vm_started):
        """Test that start() registers BOTH lifecycle AND reboot callbacks."""
        with patch('libvirt.virEventRunDefaultImpl'):
            monitor = EventMonitor(
                conn=mock_conn,
                on_vm_started=on_vm_started
            )

            await monitor.start()

            # domainEventRegisterAny should be called exactly twice
            assert mock_conn.domainEventRegisterAny.call_count == 2

            # First call: lifecycle callback
            mock_conn.domainEventRegisterAny.assert_any_call(
                None,
                libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE,
                monitor._lifecycle_callback,
                None
            )

            # Second call: reboot callback
            mock_conn.domainEventRegisterAny.assert_any_call(
                None,
                libvirt.VIR_DOMAIN_EVENT_ID_REBOOT,
                monitor._reboot_callback,
                None
            )

            # Both callback IDs should be stored
            assert 42 in monitor._callback_ids
            assert 43 in monitor._callback_ids
            assert len(monitor._callback_ids) == 2

            assert monitor._running is True

            # Cleanup
            await monitor.stop()

    def test_reboot_callback_fires_on_vm_started(self, mock_conn, on_vm_started):
        """Test that _reboot_callback calls on_vm_started with the domain.

        This is the key behavior for detecting reboots initiated via
        qemu-guest-agent, which emit a reboot event instead of a
        stop+start lifecycle pair.
        """
        monitor = EventMonitor(
            conn=mock_conn,
            on_vm_started=on_vm_started
        )

        mock_domain = Mock()
        mock_domain.name.return_value = "test-vm"

        # Invoke the reboot callback directly
        monitor._reboot_callback(mock_conn, mock_domain, None)

        # on_vm_started should have been called with the domain
        on_vm_started.assert_called_once_with(mock_domain)

    def test_reboot_callback_handles_exceptions(self, mock_conn):
        """Test that _reboot_callback catches exceptions from on_vm_started."""
        def failing_callback(domain):
            raise Exception("User callback failed during reboot")

        monitor = EventMonitor(
            conn=mock_conn,
            on_vm_started=failing_callback
        )

        mock_domain = Mock()
        mock_domain.name.return_value = "test-vm"

        # Should not raise — exception must be caught internally
        monitor._reboot_callback(mock_conn, mock_domain, None)

    @pytest.mark.asyncio
    async def test_stop_deregisters_both_callbacks(self, mock_conn, on_vm_started):
        """Test that stop() deregisters both lifecycle and reboot callback IDs."""
        with patch('libvirt.virEventRunDefaultImpl'):
            monitor = EventMonitor(
                conn=mock_conn,
                on_vm_started=on_vm_started
            )

            await monitor.start()
            await monitor.stop()

            # Both callback IDs (42 and 43) should be deregistered
            assert mock_conn.domainEventDeregisterAny.call_count == 2
            mock_conn.domainEventDeregisterAny.assert_any_call(42)
            mock_conn.domainEventDeregisterAny.assert_any_call(43)

            # Callback ID set should be cleared
            assert len(monitor._callback_ids) == 0
            assert monitor._running is False

    @pytest.mark.asyncio
    async def test_start_creates_event_loop_task(self, mock_conn, on_vm_started):
        """Test that start() creates _event_loop_task and stop() cancels it."""
        with patch('libvirt.virEventRunDefaultImpl'):
            monitor = EventMonitor(
                conn=mock_conn,
                on_vm_started=on_vm_started
            )

            # Before start, no task exists
            assert monitor._event_loop_task is None

            await monitor.start()

            # After start, task should exist and not be done
            assert monitor._event_loop_task is not None
            assert not monitor._event_loop_task.done()

            await monitor.stop()

            # After stop, task should be done/cancelled
            assert monitor._event_loop_task.done()
