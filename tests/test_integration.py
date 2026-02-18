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


# ---------------------------------------------------------------------------
# VMManager: multi-host get_vm_by_name (fall-through)
# ---------------------------------------------------------------------------

def test_vm_manager_get_vm_by_name_falls_through_hosts():
    """get_vm_by_name searches second host when first raises libvirtError."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig
    import libvirt

    # host1 doesn't have the VM, host2 does
    target_domain = Mock()
    target_domain.name.return_value = "my-vm"

    mock_conn1 = Mock()
    mock_conn1.lookupByName.side_effect = libvirt.libvirtError("Not found")
    mock_conn2 = Mock()
    mock_conn2.lookupByName.return_value = target_domain

    cfg1 = LibvirtConnectionConfig(uri="qemu:///system")
    cfg2 = LibvirtConnectionConfig(uri="qemu+ssh://host2/system")
    mgr = VMManager(connections={"host1": cfg1, "host2": cfg2})
    mgr._connections = {"host1": mock_conn1, "host2": mock_conn2}
    mgr._connected_configs = {"host1": cfg1, "host2": cfg2}

    result = mgr.get_vm_by_name("my-vm")
    assert result is target_domain
    mock_conn1.lookupByName.assert_called_once_with("my-vm")
    mock_conn2.lookupByName.assert_called_once_with("my-vm")


def test_vm_manager_get_vm_by_name_not_found_multi_host():
    """get_vm_by_name raises VMNotFoundException when no host has the VM."""
    from ansible_deployer.vm_manager import VMManager, VMNotFoundException
    from ansible_deployer.config import LibvirtConnectionConfig
    import libvirt

    mock_conn1 = Mock()
    mock_conn1.lookupByName.side_effect = libvirt.libvirtError("Not found")
    mock_conn2 = Mock()
    mock_conn2.lookupByName.side_effect = libvirt.libvirtError("Not found")

    cfg1 = LibvirtConnectionConfig(uri="qemu:///system")
    cfg2 = LibvirtConnectionConfig(uri="qemu+ssh://host2/system")
    mgr = VMManager(connections={"host1": cfg1, "host2": cfg2})
    mgr._connections = {"host1": mock_conn1, "host2": mock_conn2}
    mgr._connected_configs = {"host1": cfg1, "host2": cfg2}

    with pytest.raises(VMNotFoundException, match="not found on any"):
        mgr.get_vm_by_name("missing-vm")


# ---------------------------------------------------------------------------
# VMManager: list_vms across multiple hosts
# ---------------------------------------------------------------------------

def test_vm_manager_list_vms_multi_host():
    """list_vms returns VMs from all hosts with correct host keys."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig
    from tests.conftest import make_mock_domain, make_mock_conn

    d1 = make_mock_domain(name="host1-vm", tags=["linux-test"])
    d2 = make_mock_domain(name="host2-vm-a", tags=["linux-test"])
    d3 = make_mock_domain(name="host2-vm-b", tags=["other"])

    cfg1 = LibvirtConnectionConfig(uri="qemu:///system")
    cfg2 = LibvirtConnectionConfig(uri="qemu+ssh://host2/system")
    mgr = VMManager(connections={"host1": cfg1, "host2": cfg2})
    mgr._connections = {
        "host1": make_mock_conn([d1]),
        "host2": make_mock_conn([d2, d3]),
    }
    mgr._connected_configs = {"host1": cfg1, "host2": cfg2}

    vms = mgr.list_vms()
    assert len(vms) == 3

    # Verify host keys
    hosts = {vm["name"]: vm["host"] for vm in vms}
    assert hosts["host1-vm"] == "host1"
    assert hosts["host2-vm-a"] == "host2"
    assert hosts["host2-vm-b"] == "host2"

    # Verify config-order (host1 first, then host2)
    names = [vm["name"] for vm in vms]
    assert names == ["host1-vm", "host2-vm-a", "host2-vm-b"]


# ---------------------------------------------------------------------------
# VMManager: tag operations via domain.connect()
# ---------------------------------------------------------------------------

def test_add_vm_tag_uses_domain_connect():
    """add_vm_tag calls domain.connect() to get the right connection."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig

    mock_conn = Mock()
    mock_domain = Mock()
    mock_domain.connect.return_value = mock_conn

    cfg = LibvirtConnectionConfig(uri="test:///default")
    mgr = VMManager(connections={"default": cfg})
    mgr._connections = {"default": mock_conn}
    mgr._connected_configs = {"default": cfg}

    with patch("ansible_deployer.vm_manager.vm_ops_add_tag") as mock_add:
        mgr.add_vm_tag(mock_domain, "my-tag")

        mock_domain.connect.assert_called_once()
        mock_add.assert_called_once_with(mock_conn, mock_domain, "my-tag")


def test_remove_vm_tag_uses_domain_connect():
    """remove_vm_tag calls domain.connect() to get the right connection."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig

    mock_conn = Mock()
    mock_domain = Mock()
    mock_domain.connect.return_value = mock_conn

    cfg = LibvirtConnectionConfig(uri="test:///default")
    mgr = VMManager(connections={"default": cfg})
    mgr._connections = {"default": mock_conn}
    mgr._connected_configs = {"default": cfg}

    with patch("ansible_deployer.vm_manager.vm_ops_remove_tag") as mock_remove:
        mgr.remove_vm_tag(mock_domain, "old-tag")

        mock_domain.connect.assert_called_once()
        mock_remove.assert_called_once_with(mock_conn, mock_domain, "old-tag")


# ---------------------------------------------------------------------------
# VMManager: _get_network_for_domain and get_vm_ip per-host auto-resolve
# ---------------------------------------------------------------------------

def test_get_vm_ip_auto_resolves_per_host_network():
    """get_vm_ip(domain) uses per-host network when no explicit network given."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig

    mock_conn = Mock()
    mock_domain = Mock()
    mock_domain.connect.return_value = mock_conn

    cfg = LibvirtConnectionConfig(uri="qemu+ssh://host/system", network="mgmt-net")
    mgr = VMManager(connections={"remote": cfg})
    mgr._connections = {"remote": mock_conn}
    mgr._connected_configs = {"remote": cfg}

    with patch("ansible_deployer.vm_manager.get_vm_ip", return_value="10.0.0.50") as mock_get_ip:
        ip = mgr.get_vm_ip(mock_domain)

    assert ip == "10.0.0.50"
    # Verify the per-host network was passed
    mock_get_ip.assert_called_once_with(mock_domain, "mgmt-net")


def test_get_vm_ip_explicit_network_overrides_per_host():
    """Explicit network= argument overrides per-host config."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig

    mock_conn = Mock()
    mock_domain = Mock()
    mock_domain.connect.return_value = mock_conn

    cfg = LibvirtConnectionConfig(uri="qemu+ssh://host/system", network="mgmt-net")
    mgr = VMManager(connections={"remote": cfg})
    mgr._connections = {"remote": mock_conn}
    mgr._connected_configs = {"remote": cfg}

    with patch("ansible_deployer.vm_manager.get_vm_ip", return_value="10.0.0.60") as mock_get_ip:
        ip = mgr.get_vm_ip(mock_domain, network="custom-net")

    assert ip == "10.0.0.60"
    mock_get_ip.assert_called_once_with(mock_domain, "custom-net")


def test_get_network_for_domain_no_match():
    """_get_network_for_domain returns None when domain.connect() doesn't match any."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig

    mock_conn = Mock()
    mock_domain = Mock()
    # domain.connect() returns a DIFFERENT object than what's in _connections
    mock_domain.connect.return_value = Mock()

    cfg = LibvirtConnectionConfig(uri="test:///default", network="mgmt-net")
    mgr = VMManager(connections={"default": cfg})
    mgr._connections = {"default": mock_conn}
    mgr._connected_configs = {"default": cfg}

    with patch("ansible_deployer.vm_manager.get_vm_ip", return_value="10.0.0.1") as mock_get_ip:
        mgr.get_vm_ip(mock_domain)

    # network should be None (no match found)
    mock_get_ip.assert_called_once_with(mock_domain, None)


def test_get_network_for_domain_connect_exception():
    """_get_network_for_domain returns None when domain.connect() raises."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig

    mock_conn = Mock()
    mock_domain = Mock()
    mock_domain.connect.side_effect = Exception("connection lost")

    cfg = LibvirtConnectionConfig(uri="test:///default", network="mgmt-net")
    mgr = VMManager(connections={"default": cfg})
    mgr._connections = {"default": mock_conn}
    mgr._connected_configs = {"default": cfg}

    with patch("ansible_deployer.vm_manager.get_vm_ip", return_value="10.0.0.1") as mock_get_ip:
        mgr.get_vm_ip(mock_domain)

    # network should be None (exception path)
    mock_get_ip.assert_called_once_with(mock_domain, None)


def test_get_vm_ip_no_per_host_network_passes_none():
    """When per-host network is not configured, get_vm_ip passes None."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig

    mock_conn = Mock()
    mock_domain = Mock()
    mock_domain.connect.return_value = mock_conn

    # No network configured for this host
    cfg = LibvirtConnectionConfig(uri="qemu:///system")
    mgr = VMManager(connections={"local": cfg})
    mgr._connections = {"local": mock_conn}
    mgr._connected_configs = {"local": cfg}

    with patch("ansible_deployer.vm_manager.get_vm_ip", return_value="192.168.1.5") as mock_get_ip:
        ip = mgr.get_vm_ip(mock_domain)

    assert ip == "192.168.1.5"
    mock_get_ip.assert_called_once_with(mock_domain, None)


# ---------------------------------------------------------------------------
# VMManager: _iter_domains error handling
# ---------------------------------------------------------------------------

def test_iter_domains_skips_host_on_list_error():
    """_iter_domains logs and skips a host when listAllDomains raises."""
    from ansible_deployer.vm_manager import VMManager
    from ansible_deployer.config import LibvirtConnectionConfig
    from tests.conftest import make_mock_domain
    import libvirt

    d_good = make_mock_domain(name="good-vm", tags=["linux-test"])

    mock_conn_bad = Mock()
    mock_conn_bad.listAllDomains.side_effect = libvirt.libvirtError("host unreachable")

    mock_conn_good = Mock()
    mock_conn_good.listAllDomains.return_value = [d_good]

    cfg1 = LibvirtConnectionConfig(uri="qemu+ssh://bad/system")
    cfg2 = LibvirtConnectionConfig(uri="qemu:///system")
    mgr = VMManager(connections={"bad": cfg1, "good": cfg2})
    mgr._connections = {"bad": mock_conn_bad, "good": mock_conn_good}
    mgr._connected_configs = {"bad": cfg1, "good": cfg2}

    result = mgr._iter_domains()
    assert len(result) == 1
    assert result[0] == ("good", d_good)


# ---------------------------------------------------------------------------
# VMManager: _raise_connection_error branches
# ---------------------------------------------------------------------------

def test_raise_connection_error_refused():
    """_raise_connection_error 'refused' branch mentions libvirtd."""
    from ansible_deployer.vm_manager import VMManager

    original = Exception("Connection refused by host")
    with pytest.raises(RuntimeError, match="libvirtd service is not running"):
        VMManager._raise_connection_error("qemu:///system", "Connection refused by host", original)


def test_raise_connection_error_failed_to_connect():
    """_raise_connection_error 'failed to connect' branch mentions firewall."""
    from ansible_deployer.vm_manager import VMManager

    original = Exception("Failed to connect socket")
    with pytest.raises(RuntimeError, match="Firewall blocking"):
        VMManager._raise_connection_error("qemu+ssh://host/system", "Failed to connect socket", original)


def test_raise_connection_error_generic():
    """_raise_connection_error generic branch includes URI and error."""
    from ansible_deployer.vm_manager import VMManager

    original = Exception("Unexpected internal error")
    with pytest.raises(RuntimeError, match="qemu:///system") as exc_info:
        VMManager._raise_connection_error("qemu:///system", "Unexpected internal error", original)
    assert "Unexpected internal error" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Config: save/load round-trip with multi-host
# ---------------------------------------------------------------------------

def test_config_save_load_multihost_roundtrip(tmp_path):
    """Config with libvirt_connections survives save() + load() round-trip."""
    from ansible_deployer.config import Config, LibvirtConnectionConfig

    config_file = tmp_path / "config.yaml"

    original = Config(libvirt_connections={
        "local": LibvirtConnectionConfig(uri="qemu:///system"),
        "remote": LibvirtConnectionConfig(
            uri="qemu+ssh://root@10.0.0.5/system",
            network="mgmt-net",
        ),
    })
    original.save(config_file)

    loaded = Config.load(config_file)
    conns = loaded.get_connections()
    assert "local" in conns
    assert "remote" in conns
    assert conns["local"].uri == "qemu:///system"
    assert conns["remote"].uri == "qemu+ssh://root@10.0.0.5/system"
    assert conns["remote"].network == "mgmt-net"


def test_config_save_load_legacy_uri_roundtrip(tmp_path):
    """Legacy libvirt_uri survives save() + load() round-trip."""
    from ansible_deployer.config import Config

    config_file = tmp_path / "config.yaml"

    original = Config(libvirt_uri="qemu+ssh://test/system")
    original.save(config_file)

    loaded = Config.load(config_file)
    assert loaded.libvirt_uri == "qemu+ssh://test/system"
    conns = loaded.get_connections()
    assert conns["default"].uri == "qemu+ssh://test/system"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
