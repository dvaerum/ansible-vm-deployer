"""
Unit tests for VMTracker (session tracking and debouncing).
"""
import pytest
import asyncio
from datetime import datetime
from vm_manager.vm_tracker import VMTracker, MonitorSession


class TestVMTracker:
    """Test VMTracker session management and debouncing."""
    
    @pytest.mark.asyncio
    async def test_start_monitoring_new_vm(self):
        """Test starting monitoring for a new VM."""
        tracker = VMTracker()
        
        # Create a dummy task
        task = asyncio.create_task(asyncio.sleep(0.1))
        
        # Should successfully start monitoring
        result = await tracker.start_monitoring(
            vm_uuid="test-uuid-1",
            vm_name="test-vm-1",
            task=task
        )
        
        assert result is True
        assert await tracker.is_monitoring("test-uuid-1") is True
        
        # Cleanup
        task.cancel()
        await tracker.stop_monitoring("test-uuid-1")
    
    @pytest.mark.asyncio
    async def test_debouncing_duplicate_start(self):
        """Test debouncing - second start event for same VM is ignored."""
        tracker = VMTracker()
        
        task1 = asyncio.create_task(asyncio.sleep(0.1))
        task2 = asyncio.create_task(asyncio.sleep(0.1))
        
        # First start should succeed
        result1 = await tracker.start_monitoring(
            vm_uuid="test-uuid-1",
            vm_name="test-vm-1",
            task=task1
        )
        assert result1 is True
        
        # Second start for same UUID should fail (debouncing)
        result2 = await tracker.start_monitoring(
            vm_uuid="test-uuid-1",
            vm_name="test-vm-1",
            task=task2
        )
        assert result2 is False
        
        # Second task should have been cancelled by caller
        task2.cancel()
        
        # Cleanup
        task1.cancel()
        await tracker.stop_monitoring("test-uuid-1")
    
    @pytest.mark.asyncio
    async def test_stop_monitoring(self):
        """Test stopping monitoring removes the session."""
        tracker = VMTracker()
        
        task = asyncio.create_task(asyncio.sleep(0.1))
        
        await tracker.start_monitoring(
            vm_uuid="test-uuid-1",
            vm_name="test-vm-1",
            task=task
        )
        
        assert await tracker.is_monitoring("test-uuid-1") is True
        
        # Stop monitoring
        await tracker.stop_monitoring("test-uuid-1")
        
        assert await tracker.is_monitoring("test-uuid-1") is False
        
        # Cleanup
        task.cancel()
    
    @pytest.mark.asyncio
    async def test_get_session(self):
        """Test retrieving a monitoring session."""
        tracker = VMTracker()
        
        task = asyncio.create_task(asyncio.sleep(0.1))
        
        await tracker.start_monitoring(
            vm_uuid="test-uuid-1",
            vm_name="test-vm-1",
            task=task
        )
        
        session = await tracker.get_session("test-uuid-1")
        
        assert session is not None
        assert isinstance(session, MonitorSession)
        assert session.vm_uuid == "test-uuid-1"
        assert session.vm_name == "test-vm-1"
        assert session.task == task
        assert isinstance(session.started_at, datetime)
        
        # Cleanup
        task.cancel()
        await tracker.stop_monitoring("test-uuid-1")
    
    @pytest.mark.asyncio
    async def test_get_nonexistent_session(self):
        """Test getting a session that doesn't exist returns None."""
        tracker = VMTracker()
        
        session = await tracker.get_session("nonexistent-uuid")
        
        assert session is None
    
    @pytest.mark.asyncio
    async def test_cancel_all(self):
        """Test cancelling all active monitoring sessions."""
        tracker = VMTracker()
        
        # Start monitoring 3 VMs
        tasks = []
        for i in range(3):
            task = asyncio.create_task(asyncio.sleep(10))
            tasks.append(task)
            await tracker.start_monitoring(
                vm_uuid=f"test-uuid-{i}",
                vm_name=f"test-vm-{i}",
                task=task
            )
        
        # Verify all are being monitored
        for i in range(3):
            assert await tracker.is_monitoring(f"test-uuid-{i}") is True
        
        # Cancel all
        await tracker.cancel_all()
        
        # Verify all sessions are gone
        for i in range(3):
            assert await tracker.is_monitoring(f"test-uuid-{i}") is False
        
        # Give tasks a moment to actually cancel
        await asyncio.sleep(0.01)
        
        # Verify all tasks are cancelled
        for task in tasks:
            assert task.cancelled() or task.done()
    
    @pytest.mark.asyncio
    async def test_multiple_concurrent_vms(self):
        """Test tracking multiple VMs concurrently."""
        tracker = VMTracker()
        
        # Start monitoring 5 different VMs
        tasks = []
        for i in range(5):
            task = asyncio.create_task(asyncio.sleep(0.1))
            tasks.append(task)
            result = await tracker.start_monitoring(
                vm_uuid=f"vm-{i}",
                vm_name=f"test-vm-{i}",
                task=task
            )
            assert result is True
        
        # All should be monitored
        for i in range(5):
            assert await tracker.is_monitoring(f"vm-{i}") is True
        
        # Stop monitoring VM 2
        await tracker.stop_monitoring("vm-2")
        assert await tracker.is_monitoring("vm-2") is False
        
        # Others should still be monitored
        for i in [0, 1, 3, 4]:
            assert await tracker.is_monitoring(f"vm-{i}") is True
        
        # Cleanup
        for task in tasks:
            task.cancel()
        await tracker.cancel_all()
