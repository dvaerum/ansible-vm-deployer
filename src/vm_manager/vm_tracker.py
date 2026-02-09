"""
VM monitoring session tracker (debouncing and state management).

Tracks which VMs are currently being monitored to prevent duplicate
SSH checks when VMs reboot or send multiple start events.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class MonitorSession:
    """
    Represents an active monitoring session for a VM.
    
    Attributes:
        vm_uuid: Unique identifier for the VM
        vm_name: Name of the VM
        started_at: When monitoring started
        task: The asyncio Task handling the SSH check
    """
    vm_uuid: str
    vm_name: str
    started_at: datetime
    task: asyncio.Task


class VMTracker:
    """
    Tracks active VM monitoring sessions to prevent duplicate checks.
    
    When a VM starts, we begin monitoring it. If the VM reboots while
    we're still monitoring, we ignore the new start event (debouncing).
    """
    
    def __init__(self):
        """Initialize the tracker with an empty session registry."""
        self._sessions: Dict[str, MonitorSession] = {}
        self._lock = asyncio.Lock()
    
    async def start_monitoring(
        self,
        vm_uuid: str,
        vm_name: str,
        task: asyncio.Task
    ) -> bool:
        """
        Register a new monitoring session for a VM.
        
        Args:
            vm_uuid: Unique identifier for the VM
            vm_name: Name of the VM
            task: The asyncio Task that will monitor this VM
            
        Returns:
            True if monitoring started, False if already being monitored
        """
        async with self._lock:
            if vm_uuid in self._sessions:
                logger.debug(
                    f"VM {vm_name} (uuid={vm_uuid}) is already being monitored, "
                    "ignoring new start event (debouncing)"
                )
                return False
            
            session = MonitorSession(
                vm_uuid=vm_uuid,
                vm_name=vm_name,
                started_at=datetime.now(),
                task=task
            )
            self._sessions[vm_uuid] = session
            logger.info(f"Started monitoring VM {vm_name} (uuid={vm_uuid})")
            return True
    
    async def stop_monitoring(self, vm_uuid: str) -> None:
        """
        Remove a VM from the monitoring registry.
        
        Args:
            vm_uuid: Unique identifier for the VM to stop monitoring
        """
        async with self._lock:
            session = self._sessions.pop(vm_uuid, None)
            if session:
                logger.info(
                    f"Stopped monitoring VM {session.vm_name} (uuid={vm_uuid})"
                )
    
    async def is_monitoring(self, vm_uuid: str) -> bool:
        """
        Check if a VM is currently being monitored.
        
        Args:
            vm_uuid: Unique identifier for the VM
            
        Returns:
            True if the VM is being monitored, False otherwise
        """
        async with self._lock:
            return vm_uuid in self._sessions
    
    async def get_session(self, vm_uuid: str) -> Optional[MonitorSession]:
        """
        Get the monitoring session for a VM.
        
        Args:
            vm_uuid: Unique identifier for the VM
            
        Returns:
            The MonitorSession if found, None otherwise
        """
        async with self._lock:
            return self._sessions.get(vm_uuid)
    
    async def cancel_all(self) -> None:
        """
        Cancel all active monitoring tasks.
        
        Called during shutdown to cleanly stop all monitoring.
        """
        async with self._lock:
            logger.info(f"Cancelling {len(self._sessions)} active monitoring sessions")
            for session in self._sessions.values():
                if not session.task.done():
                    session.task.cancel()
            self._sessions.clear()
