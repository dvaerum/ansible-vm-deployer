# VM Manager Testing Documentation

Comprehensive testing documentation for VM Manager, covering unit tests, manual testing procedures, and test development guidelines.

## Table of Contents

- [Test Overview](#test-overview)
- [Running Tests](#running-tests)
- [Unit Tests](#unit-tests)
- [Manual Testing](#manual-testing)
- [Test Development](#test-development)
- [Continuous Integration](#continuous-integration)

---

## Test Overview

### Test Coverage

The project has **388 comprehensive unit tests** (214 shared/deployer + 174 vm-manager) covering all core functionality, multi-host libvirt connections, race condition fixes, broken VM handling, auto-exclude behavior, the `--on-broken` script hook (with retry/timeout/repair flow), CancelledError handling, stale tag scanning, and setMetadata API usage:

| Test File | Tests | Coverage |
|-----------|-------|----------|
| `tests/conftest.py` | Fixtures | `make_mock_domain()`, `make_mock_conn()` helpers |
| `tests/test_tag_filters.py` | 14 | `vm_matches_tags()` — required/exclude tags |
| `tests/test_vm_operations.py` | 50 | Tag CRUD, IP resolution, state strings, setMetadata API, XML fallback (includes parametrized) |
| `tests/test_metadata_manager.py` | 41 | MetadataManager get/set/claim/clear |
| `tests/test_allocate_vms.py` | 43 | VM allocation, multi-host allocation, auto-exclude broken |
| `tests/vm_manager/test_daemon.py` | 70 | Event filtering, stale tags, startup scan, stale scan loop, auto-exclude broken_tag, on-broken timeout/retries/delay init |
| `tests/vm_manager/test_tag_cleaner.py` | 59 | Race conditions #3/#6/#7, broken tag, in_use check, on-broken script, retry logic, configurable timeout, return values, repair flow, CancelledError handling, stale tag removal |
| `tests/vm_manager/test_ssh_checker.py` | 23 | Uptime verification, string return values |
| `tests/vm_manager/test_event_monitor.py` | 15 | Reboot callback registration |
| `tests/vm_manager/test_vm_tracker.py` | 7 | Session management, debouncing |
| **Total** | **388** | All race conditions, multi-host connections, broken VM handling, auto-exclude, on-broken (retry/timeout/repair), CancelledError, stale scan, setMetadata API |

### Test Types

1. **Unit Tests** (388 tests)
   - Mocked dependencies (libvirt, paramiko)
   - Fast execution (<1 minute)
   - Run automatically in CI/CD

2. **Manual Tests** (8 tests)
   - Real VMs, real libvirt
   - Validates end-to-end functionality
   - Run before releases

---

## Running Tests

### All Tests

```bash
cd /path/to/ansible-vm-deployer

# Run all tests
sudo nix develop -c python3 -m pytest tests/ -v

# Output:
# 346 passed
```

### VM Manager Tests Only

```bash
# Run vm-manager unit tests
sudo nix develop -c python3 -m pytest tests/vm_manager/ -v

# With detailed output
sudo nix develop -c python3 -m pytest tests/vm_manager/ -vv

# With coverage report
sudo nix develop -c python3 -m pytest tests/vm_manager/ \
  --cov=vm_manager \
  --cov-report=html \
  --cov-report=term
```

### Specific Test Files

```bash
# Test VMTracker only
sudo nix develop -c python3 -m pytest tests/vm_manager/test_vm_tracker.py -v

# Test SSHChecker only
sudo nix develop -c python3 -m pytest tests/vm_manager/test_ssh_checker.py -v

# Test TagCleaner only
sudo nix develop -c python3 -m pytest tests/vm_manager/test_tag_cleaner.py -v

# Test EventMonitor only
sudo nix develop -c python3 -m pytest tests/vm_manager/test_event_monitor.py -v
```

### Specific Test Methods

```bash
# Run single test
sudo nix develop -c python3 -m pytest \
  tests/vm_manager/test_vm_tracker.py::TestVMTracker::test_debouncing_duplicate_start -v

# Run tests matching pattern
sudo nix develop -c python3 -m pytest tests/vm_manager/ -k "debounce" -v
```

---

## Unit Tests

All unit tests use mocked dependencies for isolation and speed.

### test_vm_tracker.py (7 tests)

Tests session management and debouncing logic.

#### Test: start_monitoring_new_vm
```python
async def test_start_monitoring_new_vm(self):
    """Test starting monitoring for a new VM."""
```
**What it tests**: Successfully registering a new VM for monitoring  
**Validates**: Session creation, UUID tracking

#### Test: debouncing_duplicate_start
```python
async def test_debouncing_duplicate_start(self):
    """Test debouncing - second start event for same VM is ignored."""
```
**What it tests**: Debouncing prevents duplicate monitoring  
**Validates**: Second start returns False, first session remains

#### Test: stop_monitoring
```python
async def test_stop_monitoring(self):
    """Test stopping monitoring removes the session."""
```
**What it tests**: Cleanup after monitoring completes  
**Validates**: Session removed from registry

#### Test: get_session
```python
async def test_get_session(self):
    """Test retrieving a monitoring session."""
```
**What it tests**: Session retrieval by UUID  
**Validates**: MonitorSession data structure, timestamps

#### Test: cancel_all
```python
async def test_cancel_all(self):
    """Test cancelling all active monitoring sessions."""
```
**What it tests**: Shutdown cleanup  
**Validates**: All tasks cancelled, all sessions cleared

#### Test: multiple_concurrent_vms
```python
async def test_multiple_concurrent_vms(self):
    """Test tracking multiple VMs concurrently."""
```
**What it tests**: Scalability to many VMs  
**Validates**: Independent session management, selective stopping

---

### test_ssh_checker.py (23 tests)

Tests SSH connectivity verification with mocked paramiko.

#### Test: ssh_config_creation
```python
def test_ssh_config_creation(self):
    """Test creating SSH config with key."""
```
**What it tests**: SSHConfig dataclass  
**Validates**: Key path, username, port settings

#### Test: immediate_ssh_success
```python
async def test_immediate_ssh_success(self):
    """Test SSH succeeds on first attempt."""
```
**What it tests**: Happy path - immediate connection  
**Validates**: Returns True on first success

#### Test: ssh_retry_then_success
```python
async def test_ssh_retry_then_success(self):
    """Test SSH fails first, then succeeds on retry."""
```
**What it tests**: Retry logic for transient failures  
**Validates**: Retries until success, tracks attempt count

#### Test: ssh_auth_failure
```python
async def test_ssh_auth_failure(self):
    """Test SSH authentication failure (should not retry)."""
```
**What it tests**: Auth failures fail fast  
**Validates**: Returns False immediately, no retries

#### Test: ssh_timeout
```python
async def test_ssh_timeout(self):
    """Test SSH times out after max_wait_time."""
```
**What it tests**: Timeout handling  
**Validates**: Stops retrying after max_wait_time

#### Test: ssh_connect_sync_success
```python
def test_ssh_connect_sync_success(self):
    """Test synchronous SSH connection success."""
```
**What it tests**: Mocked paramiko connection  
**Validates**: Correct paramiko API calls, connection closed

#### Test: ssh_connect_with_password
```python
def test_ssh_connect_with_password(self):
    """Test SSH connection using password instead of key."""
```
**What it tests**: Password authentication  
**Validates**: Password passed to paramiko, not key

#### Test: ssh_connect_no_auth_method
```python
def test_ssh_connect_no_auth_method(self):
    """Test SSH connection with no auth method fails."""
```
**What it tests**: Configuration validation  
**Validates**: Returns auth_failure without calling connect

---

### test_tag_cleaner.py (59 tests)

Tests workflow orchestration, IP retry logic, broken VM tagging, in_use checks, and on-broken script hook.

#### Test: get_vm_ip_with_retry_success_first_try
```python
async def test_get_vm_ip_with_retry_success_first_try(self):
    """Test getting VM IP succeeds on first try."""
```
**What it tests**: Happy path IP resolution  
**Validates**: Returns IP immediately

#### Test: get_vm_ip_with_retry_after_retries
```python
async def test_get_vm_ip_with_retry_after_retries(self):
    """Test getting VM IP succeeds after retries."""
```
**What it tests**: IP retry logic  
**Validates**: Retries until IP available, tracks attempts

#### Test: get_vm_ip_with_retry_timeout
```python
async def test_get_vm_ip_with_retry_timeout(self):
    """Test getting VM IP times out after max attempts."""
```
**What it tests**: IP retry gives up eventually  
**Validates**: Returns None after exhausting attempts

#### Test: monitor_vm_success
```python
async def test_monitor_vm_success(self):
    """Test successful VM monitoring: IP -> SSH -> tag removal."""
```
**What it tests**: Complete workflow  
**Validates**: IP retrieved, SSH checked, tags removed

#### Test: monitor_vm_no_ip
```python
async def test_monitor_vm_no_ip(self):
    """Test VM monitoring stops if no IP found."""
```
**What it tests**: Failure mode - no IP  
**Validates**: SSH not called, tags not removed

#### Test: handle_vm_started_debouncing
```python
async def test_handle_vm_started_debouncing(self):
    """Test handling duplicate VM start events (debouncing)."""
```
**What it tests**: Integration with VMTracker  
**Validates**: Duplicate events don't create new tasks

#### Test: remove_multiple_tags
```python
async def test_remove_multiple_tags(self):
    """Test removing multiple tags from a VM."""
```
**What it tests**: Multiple tag removal  
**Validates**: All tags attempted, correct order

---

### test_event_monitor.py (15 tests)

Tests libvirt event loop integration and reboot callback registration.

#### Test: start_registers_callback
```python
async def test_start_registers_callback(self):
    """Test starting the monitor registers event callback."""
```
**What it tests**: Libvirt callback registration  
**Validates**: domainEventRegisterAny called correctly

#### Test: lifecycle_callback_started_event
```python
def test_lifecycle_callback_started_event(self):
    """Test lifecycle callback handles VIR_DOMAIN_EVENT_STARTED."""
```
**What it tests**: Started event handling  
**Validates**: on_vm_started callback invoked

#### Test: lifecycle_callback_stopped_event
```python
def test_lifecycle_callback_stopped_event(self):
    """Test lifecycle callback handles VIR_DOMAIN_EVENT_STOPPED."""
```
**What it tests**: Stopped event handling (boot-always)  
**Validates**: on_vm_stopped callback invoked

#### Test: lifecycle_callback_handles_exceptions
```python
def test_lifecycle_callback_handles_exceptions(self):
    """Test lifecycle callback handles exceptions in user callback."""
```
**What it tests**: Error isolation  
**Validates**: Exception in callback doesn't crash event loop

---

## Manual Testing

Manual tests validate end-to-end functionality with real VMs.

### Prerequisites

- **Running VMs**: At least one libvirt VM
- **SSH Access**: SSH keys or password configured
- **sudo Access**: Required for libvirt system connection

### Test 1: Basic Tag Removal

**Objective**: Verify tag removal after SSH success

**Steps**:
1. Add test tag to running VM:
   ```bash
   sudo virsh desc vm-name --title "tags: existing-tags, test-tag"
   ```

2. Start daemon:
   ```bash
   sudo nix develop -c python3 -m vm_manager.cli \
     --tag test-tag \
     --ssh-username root \
     --ssh-key ~/.ssh/id_ed25519 \
     --check-existing
   ```

3. Verify tag removed:
   ```bash
   sudo virsh desc vm-name
   # Should not contain "test-tag"
   ```

**Expected Result**: Tag removed after SSH check succeeds

---

### Test 2: Event Monitoring

**Objective**: Verify VM start event detection

**Steps**:
1. Add test tag to shutdown VM
2. Start daemon (without --check-existing)
3. Start the VM: `sudo virsh start vm-name`
4. Watch daemon logs

**Expected Result**: 
- "VM started and matches filters" message
- SSH check begins
- Tag removed after SSH succeeds

---

### Test 3: Debouncing

**Objective**: Verify reboot debouncing

**Steps**:
1. Add test tag, start daemon
2. Shutdown VM: `sudo virsh destroy vm-name`
3. Start VM: `sudo virsh start vm-name`
4. Quickly restart again: `sudo virsh destroy vm-name && sudo virsh start vm-name`
5. Watch logs

**Expected Result**:
- First start: "Started monitoring"
- Second start: No new "Started monitoring" (debounced)
- Tag removed once

---

### Test 4: IP Retry Logic

**Objective**: Verify IP retry after rapid restarts

**Steps**:
1. Add test tag to running VM
2. Start daemon with debug logging
3. Force VM restart: `sudo virsh destroy vm-name && sudo virsh start vm-name`
4. Watch logs for IP retry messages

**Expected Result**:
- "VM has no IP yet, retrying" messages
- Eventually "Got IP after N attempts"
- Tag removed after SSH succeeds

---

### Test 5: Boot-at-Start

**Objective**: Verify VMs boot at daemon startup

**Steps**:
1. Shutdown VMs with test tag
2. Start daemon with --boot-at-start:
   ```bash
   sudo nix develop -c python3 -m vm_manager.cli \
     --tag test-tag \
     --ssh-username root \
     --ssh-key ~/.ssh/id_ed25519 \
     --boot-at-start
   ```
3. Check VM status

**Expected Result**:
- "Booting VM: vm-name" messages
- VMs start
- SSH checks begin
- Tags removed

---

### Test 6: Boot-Always

**Objective**: Verify continuous boot loop

**Steps**:
1. Add test tag to shutdown VM
2. Start daemon with --boot-always
3. Wait for VM to boot and tag removal
4. Shutdown VM: `sudo virsh shutdown vm-name`
5. Wait ~10 seconds

**Expected Result**:
- VM boots at startup
- Tag removed after SSH
- VM reboots automatically after shutdown
- (Tag stays removed - VM no longer matches filters)

---

### Test 7: SSH Retry

**Objective**: Verify SSH retry during boot

**Steps**:
1. Shutdown VM with test tag
2. Start daemon
3. Start VM
4. Watch logs for SSH retry messages

**Expected Result**:
- "SSH connection failed... retrying" messages
- Eventually "SSH successful after Ns (N attempts)"
- Tag removed

---

### Test 8: Graceful Shutdown

**Objective**: Verify daemon handles SIGTERM

**Steps**:
1. Start daemon in foreground
2. Add test tag to running VM (triggers monitoring)
3. Send SIGTERM: `Ctrl+C` or `kill -TERM <pid>`

**Expected Result**:
- "Received signal 15" message
- "Stopping VM Manager daemon"
- "Cancelling N active monitoring sessions"
- "Daemon stopped" (clean exit)

---

## Test Development

### Adding New Unit Tests

1. **Choose appropriate test file**:
   - `test_vm_tracker.py` - Session management
   - `test_ssh_checker.py` - SSH logic
   - `test_tag_cleaner.py` - Workflow orchestration
   - `test_event_monitor.py` - Event handling

2. **Follow existing patterns**:
   ```python
   import pytest
   import asyncio
   from unittest.mock import Mock, patch
   
   class TestYourComponent:
       """Test YourComponent functionality."""
       
       @pytest.fixture
       def mock_dependency(self):
           """Create a mock dependency."""
           return Mock()
       
       @pytest.mark.asyncio
       async def test_your_feature(self, mock_dependency):
           """Test your feature with mocked dependencies."""
           # Arrange
           component = YourComponent(mock_dependency)
           
           # Act
           result = await component.do_something()
           
           # Assert
           assert result == expected_value
   ```

3. **Mock external dependencies**:
   - Use `unittest.mock.Mock` for synchronous code
   - Use `unittest.mock.AsyncMock` for async code
   - Patch at the import point, not the definition point

4. **Test async code properly**:
   ```python
   # Correct
   @pytest.mark.asyncio
   async def test_async_function(self):
       async def mock_async():
           return "success"
       
       with patch.object(obj, 'method', side_effect=mock_async):
           result = await obj.method()
           assert result == "success"
   
   # Incorrect (don't use asyncio.coroutine)
   # - Not available in Python 3.11+
   ```

### Test Naming Conventions

- Test files: `test_<component>.py`
- Test classes: `Test<Component>`
- Test methods: `test_<feature>_<scenario>`

Examples:
- `test_start_monitoring_new_vm` ✅
- `test_ssh_retry_then_success` ✅
- `test_monitor_vm_no_ip` ✅

### Mocking Best Practices

#### Mock at the Right Level

```python
# Good - mock at import point
with patch('vm_manager.tag_cleaner.get_vm_ip', return_value="192.168.1.100"):
    result = await tag_cleaner._get_vm_ip_with_retry(domain, "vm1")

# Bad - mock at definition point
with patch('vm_tools_common.vm_operations.get_vm_ip', return_value="192.168.1.100"):
    # May not work if already imported
```

#### Use Appropriate Mock Types

```python
# For simple return values
mock_obj.method = Mock(return_value="result")

# For async functions
async def mock_async():
    return "result"
mock_obj.method = Mock(side_effect=mock_async)

# For multiple calls with different results
call_count = 0
def mock_multi():
    nonlocal call_count
    call_count += 1
    if call_count < 3:
        return None
    return "result"
```

---

## Continuous Integration

### CI/CD Pipeline

The test suite is designed for CI/CD integration:

```yaml
# Example GitHub Actions workflow
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Install Nix
        uses: cachix/install-nix-action@v20
      
      - name: Run tests
        run: nix develop -c python3 -m pytest tests/vm_manager/ -v
      
      - name: Check coverage
        run: |
          nix develop -c python3 -m pytest tests/vm_manager/ \
            --cov=vm_manager \
            --cov-report=xml \
            --cov-fail-under=90
```

### Pre-commit Hooks

Run tests before committing:

```bash
# .git/hooks/pre-commit
#!/bin/bash
sudo nix develop -c python3 -m pytest tests/vm_manager/ -v
if [ $? -ne 0 ]; then
    echo "Tests failed. Commit aborted."
    exit 1
fi
```

### Test Performance

- **Unit tests**: ~43 seconds for all 346 tests
- **Fast enough** for pre-commit hooks
- **Parallelizable** for CI/CD

---

## Debugging Tests

### Run Single Test with Verbose Output

```bash
sudo nix develop -c python3 -m pytest \
  tests/vm_manager/test_ssh_checker.py::TestSSHChecker::test_ssh_retry_then_success \
  -vv \
  -s  # Don't capture stdout
```

### Enable Debug Logging in Tests

```python
import logging

def test_with_debug_logging():
    logging.basicConfig(level=logging.DEBUG)
    # Your test code
```

### Use pdb for Debugging

```python
def test_with_debugger():
    import pdb; pdb.set_trace()
    # Test code - will break here
```

Run with pytest:
```bash
sudo nix develop -c python3 -m pytest tests/vm_manager/ -s --pdb
```

---

## Coverage Reports

### Generate HTML Coverage Report

```bash
sudo nix develop -c python3 -m pytest tests/vm_manager/ \
  --cov=vm_manager \
  --cov-report=html

# Open in browser
firefox htmlcov/index.html
```

### Check Coverage Thresholds

```bash
sudo nix develop -c python3 -m pytest tests/vm_manager/ \
  --cov=vm_manager \
  --cov-fail-under=90  # Fail if coverage < 90%
```

---

## Test Maintenance

### When to Update Tests

- **API changes**: Update mocks when component interfaces change
- **New features**: Add tests for new functionality
- **Bug fixes**: Add regression tests

### Test Quality Checklist

- [ ] Tests are isolated (no dependencies on other tests)
- [ ] Tests use mocks for external dependencies
- [ ] Tests have clear, descriptive names
- [ ] Tests validate one thing each
- [ ] Tests clean up resources (async tasks, etc.)
- [ ] Tests run quickly (<1s each)

---

## Related Documentation

- [Main README](README.md) - User documentation
- [Architecture](ARCHITECTURE.md) - Design and components
