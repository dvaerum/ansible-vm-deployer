"""
Libvirt event monitoring for VM lifecycle events.

Monitors libvirt for VM start/stop events and triggers appropriate actions.
"""

import asyncio
import logging
from typing import Callable, Optional, Set
import libvirt

logger = logging.getLogger(__name__)


class EventMonitor:
    """
    Monitors libvirt domain lifecycle events.
    
    Registers callbacks for VM start/stop events and runs the libvirt
    event loop in a background thread.
    """
    
    def __init__(
        self,
        conn: libvirt.virConnect,
        on_vm_started: Callable[[libvirt.virDomain], None],
        on_vm_stopped: Optional[Callable[[libvirt.virDomain], None]] = None
    ):
        """
        Initialize the event monitor.
        
        Args:
            conn: Libvirt connection
            on_vm_started: Callback when a VM starts (called with domain object)
            on_vm_stopped: Optional callback when a VM stops (for boot-always mode)
        """
        self.conn = conn
        self.on_vm_started = on_vm_started
        self.on_vm_stopped = on_vm_stopped
        self._running = False
        self._event_loop_task: Optional[asyncio.Task] = None
        self._callback_ids: Set[int] = set()
    
    def _lifecycle_callback(
        self,
        conn: libvirt.virConnect,
        domain: libvirt.virDomain,
        event: int,
        detail: int,
        opaque
    ) -> None:
        """
        Internal callback for libvirt lifecycle events.
        
        Args:
            conn: Libvirt connection
            domain: Domain that triggered the event
            event: Event type (VIR_DOMAIN_EVENT_*)
            detail: Event detail
            opaque: User data (not used)
        """
        try:
            # VIR_DOMAIN_EVENT_STARTED = 0
            if event == libvirt.VIR_DOMAIN_EVENT_STARTED:
                logger.debug(
                    f"Received VM start event: {domain.name()} "
                    f"(detail={detail})"
                )
                if self.on_vm_started:
                    self.on_vm_started(domain)
            
            # VIR_DOMAIN_EVENT_STOPPED = 1
            elif event == libvirt.VIR_DOMAIN_EVENT_STOPPED:
                logger.debug(
                    f"Received VM stop event: {domain.name()} "
                    f"(detail={detail})"
                )
                if self.on_vm_stopped:
                    self.on_vm_stopped(domain)
        except Exception as e:
            logger.error(f"Error in lifecycle callback: {e}", exc_info=True)
    
    def _reboot_callback(
        self,
        conn: libvirt.virConnect,
        domain: libvirt.virDomain,
        opaque
    ) -> None:
        """
        Internal callback for libvirt reboot events.
        
        Args:
            conn: Libvirt connection
            domain: Domain that triggered the event
            opaque: User data (not used)
        """
        try:
            logger.debug(f"Received VM reboot event: {domain.name()}")
            if self.on_vm_started:
                self.on_vm_started(domain)
        except Exception as e:
            logger.error(f"Error in reboot callback: {e}", exc_info=True)
    
    async def start(self) -> None:
        """
        Start monitoring libvirt events.
        
        Registers event callbacks and starts the event loop.
        """
        if self._running:
            logger.warning("Event monitor is already running")
            return
        
        try:
            # Register for domain lifecycle events
            # Using None for domain means "all domains"
            callback_id = self.conn.domainEventRegisterAny(
                None,  # Monitor all domains
                libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE,
                self._lifecycle_callback,
                None  # opaque user data
            )
            self._callback_ids.add(callback_id)
            logger.info("Registered libvirt lifecycle event callback")
            
            # Register for domain reboot events
            reboot_callback_id = self.conn.domainEventRegisterAny(
                None,  # Monitor all domains
                libvirt.VIR_DOMAIN_EVENT_ID_REBOOT,
                self._reboot_callback,
                None  # opaque user data
            )
            self._callback_ids.add(reboot_callback_id)
            logger.info("Registered libvirt reboot event callback")
            
            self._running = True
            
            # Start the event loop in a background task
            self._event_loop_task = asyncio.create_task(self._run_event_loop())
            logger.info("Started libvirt event monitoring")
            
        except libvirt.libvirtError as e:
            logger.error(f"Failed to register libvirt event callback: {e}")
            raise
    
    async def _run_event_loop(self) -> None:
        """
        Run the libvirt event loop.
        
        This runs in a background asyncio task and processes libvirt events.
        """
        logger.debug("Libvirt event loop started")
        
        try:
            while self._running:
                # Process events with a timeout
                # This allows us to periodically check if we should stop
                try:
                    # runDefaultImpl() processes all pending events
                    # We need to call it regularly to keep events flowing
                    libvirt.virEventRunDefaultImpl()
                except libvirt.libvirtError as e:
                    logger.error(f"Error in libvirt event loop: {e}")
                
                # Yield control to other asyncio tasks
                await asyncio.sleep(0.1)
        
        except asyncio.CancelledError:
            logger.info("Event loop cancelled")
            raise
        except Exception as e:
            logger.error(f"Unexpected error in event loop: {e}", exc_info=True)
        finally:
            logger.debug("Libvirt event loop stopped")
    
    async def stop(self) -> None:
        """
        Stop monitoring libvirt events.
        
        Unregisters callbacks and stops the event loop.
        """
        if not self._running:
            return
        
        logger.info("Stopping libvirt event monitoring")
        self._running = False
        
        # Cancel the event loop task
        if self._event_loop_task and not self._event_loop_task.done():
            self._event_loop_task.cancel()
            try:
                await self._event_loop_task
            except asyncio.CancelledError:
                pass
        
        # Deregister event callbacks
        for callback_id in self._callback_ids:
            try:
                self.conn.domainEventDeregisterAny(callback_id)
                logger.debug(f"Deregistered event callback {callback_id}")
            except libvirt.libvirtError as e:
                logger.warning(f"Error deregistering callback {callback_id}: {e}")
        
        self._callback_ids.clear()
        logger.info("Stopped libvirt event monitoring")
