"""
Tests for VMManager internals that are not covered by test_integration.py.

Note: Basic context-manager and get_vm_by_name tests live in
test_integration.py to avoid duplication.
"""
import pytest
from unittest.mock import Mock, patch
from ansible_deployer.vm_manager import VMManager


class TestVMManager:
    """Test cases for VMManager."""

    def test_connect_closes_existing_connections_before_reconnect(self):
        """Regression: calling connect() when already connected must close
        existing handles before opening new ones (Bug 2 fix in dc054bb).

        Without the fix, the old virConnect handles would leak.
        """
        import libvirt as libvirt_mod

        vm_manager = VMManager(uri="test:///default")

        with patch("ansible_deployer.vm_manager.libvirt") as mock_libvirt:
            first_conn = Mock(name="first_conn")
            second_conn = Mock(name="second_conn")
            mock_libvirt.open.side_effect = [first_conn, second_conn]
            mock_libvirt.libvirtError = libvirt_mod.libvirtError

            # First connect — opens first_conn
            vm_manager.connect()
            assert vm_manager._connections["default"] is first_conn

            # Second connect — must close first_conn, then open second_conn
            vm_manager.connect()
            first_conn.close.assert_called_once()
            assert vm_manager._connections["default"] is second_conn

            # Cleanup
            vm_manager.disconnect()
            second_conn.close.assert_called_once()
