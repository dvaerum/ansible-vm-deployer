"""
Comprehensive tests for ansible-deployer components.
"""
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import tempfile
import os

# Test Config
def test_config_defaults():
    """Test default configuration values."""
    from ansible_deployer.config import Config
    
    config = Config()
    assert config.libvirt_uri == "qemu:///system"


def test_config_load_from_dict():
    """Test loading config from dictionary."""
    from ansible_deployer.config import Config
    
    data = {
        "libvirt_uri": "qemu+ssh://test/system",
    }
    
    config = Config(**data)
    assert config.libvirt_uri == "qemu+ssh://test/system"


def test_config_load_from_yaml(tmp_path):
    """Test loading config from YAML file."""
    from ansible_deployer.config import Config
    import yaml
    
    config_file = tmp_path / "config.yaml"
    config_data = {
        "libvirt_uri": "test:///default",
    }
    
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)
    
    config = Config.load(config_file)
    assert config.libvirt_uri == "test:///default"


# Test AnsibleExecutor
def test_ansible_executor_init(tmp_path):
    """Test AnsibleExecutor initialization."""
    from ansible_deployer.ansible_executor import AnsibleExecutor
    
    log_dir = tmp_path / "logs"
    executor = AnsibleExecutor(log_dir)
    
    assert executor.log_dir == log_dir
    assert log_dir.exists()


def test_ansible_executor_list_logs_empty(tmp_path):
    """Test listing logs when directory is empty."""
    from ansible_deployer.ansible_executor import AnsibleExecutor
    
    log_dir = tmp_path / "logs"
    executor = AnsibleExecutor(log_dir)
    
    logs = executor.list_logs()
    assert logs == []


def test_ansible_executor_list_logs_with_files(tmp_path):
    """Test listing logs with actual log files."""
    from ansible_deployer.ansible_executor import AnsibleExecutor
    
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    
    # Create test log files
    (log_dir / "20240101_120000_stdout.log").write_text("test stdout")
    (log_dir / "20240101_120000_json.log").write_text("{}")
    
    executor = AnsibleExecutor(log_dir)
    logs = executor.list_logs()
    
    assert len(logs) == 1
    assert logs[0]["task_id"] == "20240101_120000"


# Test MetadataManager (mocked)
def test_metadata_manager_mark_in_use():
    """Test marking VM as in use."""
    from ansible_deployer.metadata_manager import MetadataManager
    import libvirt
    
    mock_domain = Mock()
    mock_domain.metadata = Mock(side_effect=libvirt.libvirtError("Not found"))
    mock_domain.setMetadata = Mock()
    
    mgr = MetadataManager(mock_domain)
    mgr.mark_in_use("test-task-123")
    
    # Verify setMetadata was called
    assert mock_domain.setMetadata.called


def test_metadata_manager_mark_available():
    """Test marking VM as available."""
    from ansible_deployer.metadata_manager import MetadataManager
    import libvirt
    
    mock_domain = Mock()
    mock_domain.metadata = Mock(side_effect=libvirt.libvirtError("Not found"))
    mock_domain.setMetadata = Mock()
    
    mgr = MetadataManager(mock_domain)
    mgr.mark_available()
    
    assert mock_domain.setMetadata.called


# Test VMManager (mocked)
def test_vm_manager_init():
    """Test VMManager initialization."""
    from ansible_deployer.vm_manager import VMManager
    
    vm_mgr = VMManager("test:///default")
    assert vm_mgr.uri == "test:///default"
    assert vm_mgr.conn is None


def test_vm_manager_context_manager():
    """Test VMManager as context manager."""
    from ansible_deployer.vm_manager import VMManager
    
    with patch('ansible_deployer.vm_manager.libvirt') as mock_libvirt:
        mock_conn = Mock()
        mock_libvirt.open.return_value = mock_conn
        
        vm_mgr = VMManager("test:///default")
        with vm_mgr:
            assert vm_mgr.conn is not None
        
        mock_conn.close.assert_called_once()


def test_vm_manager_get_vm_by_name_not_found():
    """Test getting non-existent VM."""
    from ansible_deployer.vm_manager import VMManager, VMNotFoundException
    import libvirt
    
    with patch('ansible_deployer.vm_manager.libvirt') as mock_libvirt:
        mock_conn = Mock()
        mock_conn.lookupByName.side_effect = libvirt.libvirtError("Not found")
        mock_libvirt.open.return_value = mock_conn
        mock_libvirt.libvirtError = libvirt.libvirtError
        
        vm_mgr = VMManager("test:///default")
        vm_mgr.connect()
        
        with pytest.raises(VMNotFoundException):
            vm_mgr.get_vm_by_name("nonexistent")


# Integration-like tests
def test_full_workflow_simulation(tmp_path):
    """Test a simulated full workflow without actual VMs."""
    from ansible_deployer.config import Config
    from ansible_deployer.ansible_executor import AnsibleExecutor
    
    # Setup
    config = Config(log_dir=tmp_path / "logs")
    executor = AnsibleExecutor(config.log_dir)
    
    # Verify log directory was created
    assert config.log_dir.exists()
    
    # List logs (should be empty)
    logs = executor.list_logs()
    assert logs == []


def test_config_save_and_load(tmp_path):
    """Test saving and loading configuration."""
    from ansible_deployer.config import Config
    
    config_file = tmp_path / "config.yaml"
    
    # Create and save config
    config1 = Config(
        libvirt_uri="qemu+ssh://test/system",
    )
    config1.save(config_file)
    
    # Load and verify
    config2 = Config.load(config_file)
    assert config2.libvirt_uri == config1.libvirt_uri


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
