"""
Tests for VMManager.
"""
import pytest
from unittest.mock import Mock, patch
from ansible_deployer.vm_manager import VMManager, VMNotFoundException


class TestVMManager:
    """Test cases for VMManager."""

    def test_connection_context_manager(self):
        """Test connection context manager opens and closes connections."""
        import libvirt as libvirt_mod

        vm_manager = VMManager(uri="test:///default")
        
        with patch("ansible_deployer.vm_manager.libvirt") as mock_libvirt:
            mock_conn = Mock()
            mock_libvirt.open.return_value = mock_conn
            mock_libvirt.libvirtError = libvirt_mod.libvirtError
            
            with vm_manager:
                assert len(vm_manager._connections) == 1
                assert "default" in vm_manager._connections
            
            mock_conn.close.assert_called_once()
            assert vm_manager._connections == {}

    def test_get_vm_by_name_not_found(self):
        """Test getting VM that doesn't exist raises VMNotFoundException."""
        import libvirt as libvirt_mod

        vm_manager = VMManager(uri="test:///default")
        
        with patch("ansible_deployer.vm_manager.libvirt") as mock_libvirt:
            mock_conn = Mock()
            mock_libvirt.open.return_value = mock_conn
            mock_libvirt.libvirtError = libvirt_mod.libvirtError
            mock_conn.lookupByName.side_effect = libvirt_mod.libvirtError("Not found")
            
            vm_manager.connect()
            
            with pytest.raises(VMNotFoundException):
                vm_manager.get_vm_by_name("nonexistent")
