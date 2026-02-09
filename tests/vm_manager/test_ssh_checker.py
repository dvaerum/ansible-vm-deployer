"""
Unit tests for SSHChecker (SSH connectivity with retry logic).
"""
import pytest
import asyncio
from unittest.mock import Mock, patch, MagicMock
import paramiko
from vm_manager.ssh_checker import SSHChecker, SSHConfig


class TestSSHChecker:
    """Test SSH connectivity checker with mocked paramiko."""
    
    def test_ssh_config_creation(self):
        """Test creating SSH config with key."""
        config = SSHConfig(
            username="root",
            key_path="/path/to/key",
            port=22
        )
        
        assert config.username == "root"
        assert config.key_path == "/path/to/key"
        assert config.password is None
        assert config.port == 22
    
    def test_ssh_config_with_password(self):
        """Test creating SSH config with password."""
        config = SSHConfig(
            username="ansible",
            password="secret",
            port=2222
        )
        
        assert config.username == "ansible"
        assert config.password == "secret"
        assert config.key_path is None
        assert config.port == 2222
    
    @pytest.mark.asyncio
    async def test_immediate_ssh_success(self):
        """Test SSH succeeds on first attempt."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config, check_interval=1, max_wait_time=10)
        
        async def mock_success(*args):
            return "success"
        
        with patch.object(checker, '_try_ssh_connect', side_effect=mock_success):
            result = await checker.wait_for_ssh("192.168.1.100", "test-vm")
            
            assert result == "success"
    
    @pytest.mark.asyncio
    async def test_ssh_retry_then_success(self):
        """Test SSH fails first, then succeeds on retry."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config, check_interval=1, max_wait_time=10)
        
        # Mock: fail twice, succeed third time
        call_count = 0
        async def mock_try_ssh(*args):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return "connection_failed"
            return "success"
        
        with patch.object(checker, '_try_ssh_connect', side_effect=mock_try_ssh):
            result = await checker.wait_for_ssh("192.168.1.100", "test-vm")
            
            assert result == "success"
            assert call_count == 3
    
    @pytest.mark.asyncio
    async def test_ssh_auth_failure(self):
        """Test SSH authentication failure (should not retry)."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config, check_interval=1, max_wait_time=10)
        
        async def mock_auth_failure(*args):
            return "auth_failure"
        
        with patch.object(checker, '_try_ssh_connect', side_effect=mock_auth_failure):
            result = await checker.wait_for_ssh("192.168.1.100", "test-vm")
            
            assert result == "auth_failure"
    
    @pytest.mark.asyncio
    async def test_ssh_timeout(self):
        """Test SSH times out after max_wait_time."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config, check_interval=1, max_wait_time=3)
        
        # Always fail
        async def mock_failure(*args):
            return "connection_failed"
        
        with patch.object(checker, '_try_ssh_connect', side_effect=mock_failure):
            result = await checker.wait_for_ssh("192.168.1.100", "test-vm")
            
            assert result == "timeout"
    
    @pytest.mark.asyncio
    async def test_ssh_no_timeout(self):
        """Test SSH with no max_wait_time (infinite retry)."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config, check_interval=1, max_wait_time=None)
        
        # Mock: fail 5 times, then succeed
        call_count = 0
        async def mock_try_ssh(*args):
            nonlocal call_count
            call_count += 1
            if call_count < 6:
                return "connection_failed"
            return "success"
        
        with patch.object(checker, '_try_ssh_connect', side_effect=mock_try_ssh):
            result = await checker.wait_for_ssh("192.168.1.100", "test-vm")
            
            assert result == "success"
            assert call_count == 6
    
    def test_ssh_connect_sync_success(self):
        """Test synchronous SSH connection success with fresh boot uptime."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config)
        
        with patch('paramiko.SSHClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            
            # Mock successful connection
            mock_client.connect.return_value = None
            
            # Mock uptime check (fresh boot: 10 seconds uptime)
            mock_stdout = MagicMock()
            mock_stdout.read.return_value = b"10.5 123.4"
            mock_client.exec_command.return_value = (MagicMock(), mock_stdout, MagicMock())
            
            result = checker._ssh_connect_sync("192.168.1.100", "test-vm", 1)
            
            assert result == "success"
            mock_client.connect.assert_called_once()
            mock_client.close.assert_called_once()
    
    def test_ssh_connect_sync_auth_failure(self):
        """Test synchronous SSH connection with auth failure."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config)
        
        with patch('paramiko.SSHClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            
            # Mock authentication failure
            mock_client.connect.side_effect = paramiko.AuthenticationException("Auth failed")
            
            result = checker._ssh_connect_sync("192.168.1.100", "test-vm", 1)
            
            assert result == "auth_failure"
            mock_client.close.assert_called_once()
    
    def test_ssh_connect_sync_connection_refused(self):
        """Test synchronous SSH connection refused (retryable)."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config)
        
        with patch('paramiko.SSHClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            
            # Mock connection refused
            mock_client.connect.side_effect = ConnectionRefusedError("Connection refused")
            
            result = checker._ssh_connect_sync("192.168.1.100", "test-vm", 1)
            
            assert result == "connection_failed"
            mock_client.close.assert_called_once()
    
    def test_ssh_connect_with_password(self):
        """Test SSH connection using password instead of key."""
        config = SSHConfig(username="root", password="secret")
        checker = SSHChecker(config)
        
        with patch('paramiko.SSHClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            
            result = checker._ssh_connect_sync("192.168.1.100", "test-vm", 1)
            
            # Verify password was passed to connect
            call_args = mock_client.connect.call_args
            assert call_args[1]['password'] == "secret"
            assert 'key_filename' not in call_args[1]
    
    def test_ssh_connect_with_key(self):
        """Test SSH connection using key file."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config)
        
        with patch('paramiko.SSHClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            
            result = checker._ssh_connect_sync("192.168.1.100", "test-vm", 1)
            
            # Verify key_filename was passed to connect
            call_args = mock_client.connect.call_args
            assert call_args[1]['key_filename'] == "/test/key"
            assert 'password' not in call_args[1]
    
    def test_ssh_connect_no_auth_method(self):
        """Test SSH connection with no auth method fails."""
        config = SSHConfig(username="root")  # No key or password
        checker = SSHChecker(config)
        
        with patch('paramiko.SSHClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            
            result = checker._ssh_connect_sync("192.168.1.100", "test-vm", 1)
            
            assert result == "auth_failure"
            # connect should not be called
            mock_client.connect.assert_not_called()

    # ---- Uptime verification tests (race condition #2 fix) ----

    def test_ssh_connect_sync_fresh_boot_uptime(self):
        """Test uptime < 120s is treated as fresh boot -> success."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config)

        with patch('paramiko.SSHClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            # Mock successful connection
            mock_client.connect.return_value = None

            # Mock exec_command for uptime check: 5.3 seconds uptime
            mock_stdout = MagicMock()
            mock_stdout.read.return_value = b"5.3 123.4"
            mock_client.exec_command.return_value = (MagicMock(), mock_stdout, MagicMock())

            result = checker._ssh_connect_sync("192.168.1.100", "test-vm", 1)

            assert result == "success"
            mock_client.exec_command.assert_called_once_with("cat /proc/uptime", timeout=5)
            mock_client.close.assert_called_once()

    def test_ssh_connect_sync_stale_boot_uptime(self):
        """Test uptime > 120s is treated as stale boot -> connection_failed (will retry)."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config)

        with patch('paramiko.SSHClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            # Mock successful connection
            mock_client.connect.return_value = None

            # Mock exec_command for uptime check: 500 seconds uptime (stale)
            mock_stdout = MagicMock()
            mock_stdout.read.return_value = b"500.0 1234.5"
            mock_client.exec_command.return_value = (MagicMock(), mock_stdout, MagicMock())

            result = checker._ssh_connect_sync("192.168.1.100", "test-vm", 1)

            assert result == "connection_failed"
            mock_client.exec_command.assert_called_once_with("cat /proc/uptime", timeout=5)
            mock_client.close.assert_called_once()

    def test_ssh_connect_sync_uptime_boundary(self):
        """Test uptime exactly at 120s boundary is treated as stale."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config)

        with patch('paramiko.SSHClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            mock_client.connect.return_value = None

            # Mock uptime at exactly 120 seconds (not < 120, so stale)
            mock_stdout = MagicMock()
            mock_stdout.read.return_value = b"120.0 500.0"
            mock_client.exec_command.return_value = (MagicMock(), mock_stdout, MagicMock())

            result = checker._ssh_connect_sync("192.168.1.100", "test-vm", 1)

            assert result == "connection_failed"

    def test_ssh_connect_sync_uptime_just_under_boundary(self):
        """Test uptime just under 120s is treated as fresh boot."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config)

        with patch('paramiko.SSHClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            mock_client.connect.return_value = None

            # Mock uptime at 119.9 seconds (< 120, so fresh)
            mock_stdout = MagicMock()
            mock_stdout.read.return_value = b"119.9 400.0"
            mock_client.exec_command.return_value = (MagicMock(), mock_stdout, MagicMock())

            result = checker._ssh_connect_sync("192.168.1.100", "test-vm", 1)

            assert result == "success"

    def test_ssh_connect_sync_uptime_check_exception(self):
        """Test exec_command raising exception -> connection_failed (retry)."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config)

        with patch('paramiko.SSHClient') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            # Connection succeeds but uptime check fails
            mock_client.connect.return_value = None
            mock_client.exec_command.side_effect = Exception("Channel closed")

            result = checker._ssh_connect_sync("192.168.1.100", "test-vm", 1)

            assert result == "connection_failed"
            mock_client.close.assert_called_once()

    # ---- Tests verifying wait_for_ssh returns string values ----

    @pytest.mark.asyncio
    async def test_wait_for_ssh_returns_timeout_string(self):
        """Test wait_for_ssh returns 'timeout' string (not False) when time exceeded."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config, check_interval=1, max_wait_time=2)

        async def mock_failure(*args):
            return "connection_failed"

        with patch.object(checker, '_try_ssh_connect', side_effect=mock_failure):
            result = await checker.wait_for_ssh("192.168.1.100", "test-vm")

            # Must be the string "timeout", not a boolean
            assert result == "timeout"
            assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_wait_for_ssh_returns_auth_failure_string(self):
        """Test wait_for_ssh returns 'auth_failure' string (not False) on auth error."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config, check_interval=1, max_wait_time=10)

        async def mock_auth_failure(*args):
            return "auth_failure"

        with patch.object(checker, '_try_ssh_connect', side_effect=mock_auth_failure):
            result = await checker.wait_for_ssh("192.168.1.100", "test-vm")

            # Must be the string "auth_failure", not a boolean
            assert result == "auth_failure"
            assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_wait_for_ssh_returns_success_string(self):
        """Test wait_for_ssh returns 'success' string (not True) on success."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config, check_interval=1, max_wait_time=10)

        async def mock_success(*args):
            return "success"

        with patch.object(checker, '_try_ssh_connect', side_effect=mock_success):
            result = await checker.wait_for_ssh("192.168.1.100", "test-vm")

            # Must be the string "success", not a boolean
            assert result == "success"
            assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_wait_for_ssh_timeout_triggers_broken_tag(self):
        """Test that 'timeout' return value is the string that triggers broken tag in TagCleaner."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config, check_interval=1, max_wait_time=2)

        async def mock_always_fail(*args):
            return "connection_failed"

        with patch.object(checker, '_try_ssh_connect', side_effect=mock_always_fail):
            result = await checker.wait_for_ssh("192.168.1.100", "test-vm")

            # Verify exact string comparison used by TagCleaner
            assert result == "timeout"
            assert result != True  # noqa: E712
            assert result != False  # noqa: E712

    @pytest.mark.asyncio
    async def test_stale_uptime_causes_retry_in_wait_loop(self):
        """Integration: stale uptime causes retry loop until fresh boot detected."""
        config = SSHConfig(username="root", key_path="/test/key")
        checker = SSHChecker(config, check_interval=0, max_wait_time=10)

        # First two calls return stale uptime, third returns fresh boot
        call_count = 0

        async def mock_try_ssh(*args):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return "connection_failed"  # Stale uptime results in this
            return "success"

        with patch.object(checker, '_try_ssh_connect', side_effect=mock_try_ssh):
            result = await checker.wait_for_ssh("192.168.1.100", "test-vm")

            assert result == "success"
            assert call_count == 3