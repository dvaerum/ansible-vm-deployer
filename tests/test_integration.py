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
    """Test default configuration values (legacy libvirt_uri is None, get_connections() provides the default)."""
    from ansible_deployer.config import Config
    
    config = Config()
    assert config.libvirt_uri is None
    assert config.libvirt_connections is None

    # get_connections() should return the fallback default
    conns = config.get_connections()
    assert "default" in conns
    assert conns["default"].uri == "qemu:///system"


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
    """Test VMManager initialization with uri= shorthand."""
    from ansible_deployer.vm_manager import VMManager
    
    vm_mgr = VMManager(uri="test:///default")
    assert "default" in vm_mgr._connection_configs
    assert vm_mgr._connection_configs["default"].uri == "test:///default"
    assert vm_mgr._connections == {}


def test_vm_manager_init_default():
    """Test VMManager initialization with no arguments (qemu:///system default)."""
    from ansible_deployer.vm_manager import VMManager

    vm_mgr = VMManager()
    assert "default" in vm_mgr._connection_configs
    assert vm_mgr._connection_configs["default"].uri == "qemu:///system"


def test_vm_manager_init_connections():
    """Test VMManager initialization with named connections."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig

    conns = {
        "local": LibvirtConnectionConfig(uri="qemu:///system"),
        "remote": LibvirtConnectionConfig(uri="qemu+ssh://root@10.0.0.5/system", network="mgmt"),
    }
    vm_mgr = VMManager(connections=conns)
    assert vm_mgr._connection_configs is conns
    assert "local" in vm_mgr._connection_configs
    assert "remote" in vm_mgr._connection_configs
    assert vm_mgr._connection_configs["remote"].network == "mgmt"


def test_vm_manager_init_rejects_string():
    """Passing a string as connections should raise TypeError with helpful message."""
    from ansible_deployer.vm_manager import VMManager

    with pytest.raises(TypeError, match="Use VMManager"):
        VMManager(connections="qemu:///system")


def test_vm_manager_context_manager():
    """Test VMManager as context manager (connect + disconnect)."""
    from ansible_deployer.vm_manager import VMManager
    import libvirt

    with patch('ansible_deployer.vm_manager.libvirt') as mock_libvirt:
        mock_conn = Mock()
        mock_libvirt.open.return_value = mock_conn
        mock_libvirt.libvirtError = libvirt.libvirtError
        
        vm_mgr = VMManager(uri="test:///default")
        with vm_mgr:
            assert len(vm_mgr._connections) == 1
            assert "default" in vm_mgr._connections
        
        # After exit, connections should be closed and cleared
        mock_conn.close.assert_called_once()
        assert vm_mgr._connections == {}


def test_vm_manager_get_vm_by_name_not_found():
    """Test getting non-existent VM raises VMNotFoundException."""
    from ansible_deployer.vm_manager import VMManager, VMNotFoundException
    import libvirt
    
    with patch('ansible_deployer.vm_manager.libvirt') as mock_libvirt:
        mock_conn = Mock()
        mock_conn.lookupByName.side_effect = libvirt.libvirtError("Not found")
        mock_libvirt.open.return_value = mock_conn
        mock_libvirt.libvirtError = libvirt.libvirtError
        
        vm_mgr = VMManager(uri="test:///default")
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


# ---------------------------------------------------------------------------
# Config: get_connections() resolution
# ---------------------------------------------------------------------------

def test_config_get_connections_from_legacy_uri():
    """Legacy libvirt_uri is wrapped as a single 'default' connection."""
    from ansible_deployer.config import Config

    config = Config(libvirt_uri="qemu+ssh://admin@10.0.0.5/system")
    conns = config.get_connections()
    assert list(conns.keys()) == ["default"]
    assert conns["default"].uri == "qemu+ssh://admin@10.0.0.5/system"
    assert conns["default"].network is None


def test_config_get_connections_from_multi_host():
    """libvirt_connections dict is returned as-is."""
    from ansible_deployer.config import Config, LibvirtConnectionConfig

    config = Config(libvirt_connections={
        "local": LibvirtConnectionConfig(uri="qemu:///system"),
        "remote": LibvirtConnectionConfig(uri="qemu+ssh://root@10.0.0.5/system", network="mgmt"),
    })
    conns = config.get_connections()
    assert list(conns.keys()) == ["local", "remote"]
    assert conns["remote"].network == "mgmt"


def test_config_get_connections_precedence():
    """When both libvirt_uri and libvirt_connections are set, connections wins."""
    from ansible_deployer.config import Config, LibvirtConnectionConfig

    config = Config(
        libvirt_uri="qemu:///system",
        libvirt_connections={
            "host-a": LibvirtConnectionConfig(uri="qemu+ssh://a/system"),
        },
    )
    conns = config.get_connections()
    assert "host-a" in conns
    assert "default" not in conns


def test_config_loaded_from_explicit_path(tmp_path):
    """Config.loaded_from reports the file that was loaded."""
    from ansible_deployer.config import Config
    import yaml

    config_file = tmp_path / "my-config.yaml"
    with open(config_file, "w") as f:
        yaml.dump({"libvirt_uri": "test:///default"}, f)

    config = Config.load(config_file)
    assert config.loaded_from == config_file


def test_config_loaded_from_none_when_no_file():
    """Config.loaded_from is None when no config file was found."""
    from ansible_deployer.config import Config

    # Load from a non-existent directory so no file is found
    config = Config.load(project_root=Path("/nonexistent/path"))
    assert config.loaded_from is None


def test_config_search_paths_include_project_root():
    """_default_search_paths includes project_root entries when set."""
    from ansible_deployer.config import Config

    paths = Config._default_search_paths(project_root=Path("/my/project"))
    path_strs = [str(p) for p in paths]
    assert "/my/project/config.yaml" in path_strs
    assert "/my/project/config.yml" in path_strs


def test_config_search_paths_without_project_root():
    """_default_search_paths omits project_root entries when not set."""
    from ansible_deployer.config import Config

    paths = Config._default_search_paths(project_root=None)
    path_strs = [str(p) for p in paths]
    # Should have cwd, home, and system paths but no project_root entries
    assert "config.yaml" in path_strs
    assert "config.yml" in path_strs
    assert any("/etc/ansible-deployer" in p for p in path_strs)


def test_config_load_yaml_with_multi_host(tmp_path):
    """Config.load() can parse a multi-host YAML config."""
    from ansible_deployer.config import Config
    import yaml

    config_file = tmp_path / "config.yaml"
    data = {
        "libvirt_connections": {
            "local": {"uri": "qemu:///system"},
            "remote": {"uri": "qemu+ssh://root@10.0.0.5/system", "network": "mgmt"},
        }
    }
    with open(config_file, "w") as f:
        yaml.dump(data, f)

    config = Config.load(config_file)
    conns = config.get_connections()
    assert "local" in conns
    assert "remote" in conns
    assert conns["remote"].network == "mgmt"


# ---------------------------------------------------------------------------
# VMManager: multi-host connection management
# ---------------------------------------------------------------------------

def test_vm_manager_connect_single_host_failure():
    """Single-host connect failure raises RuntimeError with helpful message."""
    from ansible_deployer.vm_manager import VMManager
    import libvirt

    with patch("ansible_deployer.vm_manager.libvirt") as mock_libvirt:
        mock_libvirt.open.side_effect = libvirt.libvirtError("auth failed polkit")
        mock_libvirt.libvirtError = libvirt.libvirtError

        mgr = VMManager(uri="qemu:///system")
        with pytest.raises(RuntimeError, match="authentication"):
            mgr.connect()


def test_vm_manager_connect_multi_host_partial_failure():
    """When one of multiple hosts fails, the other still works."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig
    import libvirt

    mock_conn = Mock()

    def open_side_effect(uri):
        if "bad-host" in uri:
            raise libvirt.libvirtError("Connection refused")
        return mock_conn

    with patch("ansible_deployer.vm_manager.libvirt") as mock_libvirt:
        mock_libvirt.open.side_effect = open_side_effect
        mock_libvirt.libvirtError = libvirt.libvirtError

        mgr = VMManager(connections={
            "good": LibvirtConnectionConfig(uri="qemu:///system"),
            "bad": LibvirtConnectionConfig(uri="qemu+ssh://bad-host/system"),
        })
        mgr.connect()

        # Only the good host should be connected
        assert "good" in mgr._connections
        assert "bad" not in mgr._connections


def test_vm_manager_connect_multi_host_all_fail():
    """When all hosts fail, RuntimeError is raised."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig
    import libvirt

    with patch("ansible_deployer.vm_manager.libvirt") as mock_libvirt:
        mock_libvirt.open.side_effect = libvirt.libvirtError("Connection refused")
        mock_libvirt.libvirtError = libvirt.libvirtError

        mgr = VMManager(connections={
            "host-a": LibvirtConnectionConfig(uri="qemu+ssh://a/system"),
            "host-b": LibvirtConnectionConfig(uri="qemu+ssh://b/system"),
        })
        with pytest.raises(RuntimeError, match="Failed to connect to any"):
            mgr.connect()


def test_vm_manager_list_vms_includes_host_key():
    """list_vms() includes a 'host' key in each VM dict."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig
    from tests.conftest import make_mock_domain, make_mock_conn

    domain = make_mock_domain(name="test-vm-1", tags=["linux-test"])
    mock_conn = make_mock_conn([domain])

    cfg = LibvirtConnectionConfig(uri="test:///default")
    mgr = VMManager(connections={"my-host": cfg})
    mgr._connections = {"my-host": mock_conn}
    mgr._connected_configs = {"my-host": cfg}

    vms = mgr.list_vms()
    assert len(vms) == 1
    assert vms[0]["host"] == "my-host"
    assert vms[0]["name"] == "test-vm-1"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
