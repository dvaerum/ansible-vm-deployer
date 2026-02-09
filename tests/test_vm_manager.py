"""
Tests for VMManager.
"""
import pytest
from unittest.mock import Mock, patch
from ansible_deployer.vm_manager import VMManager, VMNotFoundException


class TestVMManager:
    """Test cases for VMManager."""

    def test_connection_context_manager(self):
        """Test connection context manager."""
        vm_manager = VMManager("test:///default")
        
        with patch("libvirt.open") as mock_open:
            mock_conn = Mock()
            mock_open.return_value = mock_conn
            
            with vm_manager:
                assert vm_manager.conn is not None
            
            mock_conn.close.assert_called_once()

    def test_get_vm_by_name_not_found(self):
        """Test getting VM that doesn't exist."""
        import libvirt as libvirt_mod
        vm_manager = VMManager("test:///default")
        
        with patch("libvirt.open") as mock_open:
            mock_conn = Mock()
            mock_open.return_value = mock_conn
            mock_conn.lookupByName.side_effect = libvirt_mod.libvirtError("Not found")
            
            vm_manager.connect()
            
            with pytest.raises(VMNotFoundException):
                vm_manager.get_vm_by_name("nonexistent")