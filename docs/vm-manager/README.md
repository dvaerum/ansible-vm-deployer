# VM Manager

A production-ready daemon that monitors libvirt VMs, waits for SSH connectivity, and automatically removes tags when VMs are ready. Designed for automated VM lifecycle management with robust error handling and comprehensive test coverage.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [CLI Reference](#cli-reference)
- [Use Cases](#use-cases)
- [Architecture](#architecture)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## Overview

VM Manager is an event-driven daemon that:

1. **Monitors** libvirt domains for lifecycle events (start/stop)
2. **Waits** for VMs to become accessible via SSH (with full authentication)
3. **Removes** specified tags from VM metadata when SSH succeeds
4. **Boots** VMs automatically based on tags (optional)

This tool is ideal for automated provisioning pipelines where you need to know when a VM is truly ready for automation, not just when it has started.

---

## Features

### Core Functionality

- ✅ **Event-Driven Monitoring**: React to VM lifecycle and reboot events in real-time
- ✅ **SSH Verification**: Full SSH authentication with uptime verification (< 120s)
- ✅ **Automatic Tag Removal**: Remove tags when VMs are ready (with safety checks)
- ✅ **In-Use Protection**: Checks `in_use` metadata before removing tags to prevent mid-run removal
- ✅ **Broken VM Tagging**: VMs that fail SSH after timeout are tagged `broken` for visibility
- ✅ **Auto-Exclude Broken VMs**: Daemon auto-excludes broken VMs from monitoring; deployer auto-excludes them from allocation
- ✅ **On-Broken Script Hook**: Optional external script called when a VM is marked broken (`--on-broken`)
- ✅ **Periodic Stale Tag Scan**: Background loop detects and removes stale `used` tags from VMs that were never rebooted after deploy
- ✅ **Retry Logic**: Intelligent retry for both SSH and IP address resolution (skips loopback IPs)
- ✅ **Debouncing**: Prevents duplicate processing during VM reboots
- ✅ **Parallel Processing**: Handle multiple VMs concurrently with asyncio

### Boot Modes

- ✅ **--boot-at-start**: Boot all matching shutdown VMs once at daemon startup
- ✅ **--boot-always**: Continuously boot matching shutdown VMs (daemon monitors and reboots)

### Operational Features

- ✅ **--check-existing**: Process already-running VMs at startup
- ✅ **Graceful Shutdown**: Handle SIGTERM/SIGINT cleanly
- ✅ **Configurable Timeouts**: Control SSH retry intervals and max wait times
- ✅ **Tag Filtering**: Monitor specific tags and exclude others
- ✅ **Debug Logging**: Comprehensive logging at multiple levels

---

## Installation

The tool is part of the `ansible-vm-deployer` repository and is available in the Nix development environment:

```bash
cd /path/to/ansible-vm-deployer
sudo nix develop
```

The `vm-manager` command is available within the Nix shell.

### Requirements

- **Nix** package manager
- **libvirt** (qemu:///system or custom URI)
- **SSH access** to VMs (key-based or password-based)
- **Python 3.11+** (provided by Nix)

---

## Quick Start

### Basic Usage

Monitor VMs with the "used" tag, remove it when SSH is ready:

```bash
sudo nix develop -c python3 -m vm_manager.cli \
  --tag used \
  --ssh-username root \
  --ssh-key ~/.ssh/id_ed25519 \
  --libvirt-uri qemu:///system
```

### With Boot-at-Start

Boot all matching shutdown VMs once at startup:

```bash
sudo nix develop -c python3 -m vm_manager.cli \
  --tag provision-me \
  --ssh-username root \
  --ssh-key ~/.ssh/id_ed25519 \
  --boot-at-start \
  --check-existing
```

### Continuous Boot Loop

Keep booting matching VMs whenever they shut down:

```bash
sudo nix develop -c python3 -m vm_manager.cli \
  --tag auto-boot \
  --ssh-username ansible \
  --ssh-key ~/.ssh/ansible_key \
  --boot-always \
  --check-interval 10
```

---

## CLI Reference

### Required Arguments

| Argument | Description |
|----------|-------------|
| `--tag TAG` | VM tag to monitor (can specify multiple times) |
| `--ssh-username USER` | SSH username for connectivity checks |

### SSH Authentication (at least one required)

| Argument | Description |
|----------|-------------|
| `--ssh-key PATH` | Path to SSH private key file |
| `--ssh-password-file PATH` | Path to file containing SSH password |

### Optional Tag Filtering

| Argument | Description |
|----------|-------------|
| `--exclude-tag TAG` | Exclude VMs with this tag (can specify multiple times) |
| `--mark-as-used [TAG]` | Tag to remove (default: "used" if flag provided without value) |

### Boot Modes (mutually exclusive)

| Argument | Description |
|----------|-------------|
| `--boot-at-start` | Boot all matching shutdown VMs once at startup |
| `--boot-always` | Continuously boot matching shutdown VMs |

### Timing Options

| Argument | Default | Description |
|----------|---------|-------------|
| `--check-interval SECONDS` | 10 | Interval between SSH retry attempts |
| `--max-wait-time SECONDS` | 1800 | Maximum time to wait for SSH (30 minutes) |

### Broken VM Tagging

| Argument | Default | Description |
|----------|---------|-------------|
| `--broken-tag TAG` | broken | Tag to add when SSH times out after max-wait-time |
| `--no-broken-tag` | | Don't add a tag on timeout (just stop monitoring) |
| `--on-broken PATH` | | External script to run when a VM is marked broken |
| `--on-broken-timeout SECONDS` | 300 | Max time before killing the on-broken script |
| `--on-broken-retries COUNT` | unlimited | Max retry attempts (omit for infinite retries) |
| `--on-broken-retry-delay SECONDS` | 60 | Delay between on-broken script retries |

When a VM fails SSH checks for `--max-wait-time` seconds, it is tagged with `--broken-tag` (default: `broken`). The `used` tag is intentionally kept so the VM won't be reallocated by ansible-deployer.

**Auto-exclude behavior**: The daemon automatically appends the `broken_tag` to its `exclude_tags` list, preventing infinite re-monitoring loops. The ansible-deployer also auto-excludes VMs tagged `broken` from allocation.

**On-broken script hook**: If `--on-broken /path/to/handler.sh` is specified, the script is called asynchronously when a VM is marked broken. The script receives VM information via environment variables:

| Variable | Example | Description |
|----------|---------|-------------|
| `VM_NAME` | `linux-vm-07` | VM name |
| `VM_UUID` | `a1b2c3d4-...` | Libvirt UUID |
| `VM_IP` | `10.0.0.7` | Last known IP (or empty) |
| `VM_TAGS` | `linux-test,used,broken` | Comma-separated current tags |
| `VM_BROKEN_TAG` | `broken` | The broken tag that was added |
| `VM_WAIT_TIME` | `1800` | Max wait time in seconds |
| `LIBVIRT_URI` | `qemu:///system` | Libvirt connection URI |

The script timeout is configurable via `--on-broken-timeout` (default: 300 seconds). The script retries on failure with configurable retry count (`--on-broken-retries`, default: unlimited) and delay (`--on-broken-retry-delay`, default: 60 seconds). Non-zero exit codes are logged as warnings but don't affect vm-manager operation.

### Stale Tag Scanning

| Argument | Default | Description |
|----------|---------|-------------|
| `--stale-scan-interval SECONDS` | 300 | Interval between periodic stale tag scans (0 = disabled) |

Periodically scans all running VMs and removes stale `used` tags directly (without SSH). A tag is stale when the VM has a removable tag in its inactive XML but is no longer actively in use (`in_use=false` or no metadata). This catches VMs where the deployer finished but the VM was never rebooted.

### Startup Behavior

| Argument | Description |
|----------|-------------|
| `--check-existing` | Check existing running VMs at startup (actively in-use VMs go through SSH monitoring; stale tags are removed directly) |

### Connection & Logging

| Argument | Default | Description |
|----------|---------|-------------|
| `--libvirt-uri URI` | qemu:///system | Libvirt connection URI |
| `--log-level LEVEL` | info | Log level (debug/info/warning/error) |

---

## Use Cases

### 1. Automated Provisioning Pipeline

**Scenario**: You have a provisioning system that:
1. Creates VMs with a "provision-me" tag
2. Needs to know when VMs are SSH-accessible
3. Runs Ansible playbooks after VMs are ready

**Solution**:
```bash
vm-manager \
  --tag provision-me \
  --ssh-username root \
  --ssh-key /keys/provisioning.key \
  --mark-as-used provision-me
```

When the tag is removed, trigger your Ansible playbooks.

### 2. Test Environment Management

**Scenario**: You have test VMs that should:
1. Auto-boot when they shut down
2. Be monitored for SSH readiness
3. Have their "used" tag cleared when ready

**Solution**:
```bash
vm-manager \
  --tag test-vm \
  --ssh-username test \
  --ssh-key ~/.ssh/test_key \
  --boot-always \
  --check-existing
```

### 3. CI/CD Integration

**Scenario**: Your CI/CD pipeline:
1. Spins up fresh VMs for each test run
2. Tags them with "ci-test"
3. Needs to wait for SSH before running tests

**Solution**:
```bash
vm-manager \
  --tag ci-test \
  --ssh-username ci \
  --ssh-key /run/secrets/ci-ssh-key \
  --max-wait-time 300 \
  --log-level debug
```

### 4. Development Environment

**Scenario**: Developers work with VMs that:
1. Should auto-start when the host boots
2. Need their "ready" tag removed when accessible
3. Exclude VMs tagged "production"

**Solution**:
```bash
vm-manager \
  --tag dev-vm \
  --exclude-tag production \
  --ssh-username dev \
  --ssh-key ~/.ssh/dev_key \
  --boot-at-start \
  --check-existing
```

---

## Architecture

For detailed architecture documentation, see [ARCHITECTURE.md](ARCHITECTURE.md).

### High-Level Components

```
┌─────────────┐
│   Daemon    │  - Orchestrates all components
│             │  - Signal handling (SIGTERM/SIGINT)
│             │  - Lifecycle management
└──────┬──────┘
       │
       ├──────────────┬──────────────┬──────────────┐
       ▼              ▼              ▼              ▼
┌─────────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐
│EventMonitor │ │SSHChecker│ │VMTracker │ │ TagCleaner   │
│             │ │          │ │          │ │              │
│- Libvirt    │ │- Paramiko│ │- Session │ │- Orchestrate │
│  events     │ │- Retry   │ │  mgmt    │ │  workflow    │
│- Callbacks  │ │- Timeout │ │- Debounce│ │- Remove tags │
└─────────────┘ └──────────┘ └──────────┘ └──────────────┘
```

### Workflow

1. **Event Detection**: `EventMonitor` detects VM reboot/start event from libvirt
2. **Tag Pre-check**: Daemon verifies VM has removable tags (e.g., `used`), skips if not
3. **Debouncing**: `VMTracker` checks if VM is already being monitored
4. **IP Resolution**: `TagCleaner` gets VM IP with retry logic (skips loopback `127.*`)
5. **SSH Check**: `SSHChecker` waits for SSH with uptime < 120s verification
6. **Timeout Handling**: If SSH times out, VM is tagged `broken` (configurable), then `--on-broken` script is called if configured
7. **In-Use Check**: `TagCleaner` verifies no deployer session is active (`in_use` metadata)
8. **Tag Removal**: `TagCleaner` removes specified tags when all checks pass
9. **Cleanup**: `VMTracker` stops monitoring the VM

**Note**: Broken VMs are automatically excluded from future monitoring (daemon auto-appends `broken_tag` to `exclude_tags`) and from ansible-deployer allocation.

---

## Testing

The project has comprehensive test coverage:

- **334 total tests** (100% pass rate)
  - Covers all 7 race condition fixes, broken VM handling, auto-exclude behavior, `--on-broken` script hook (with retry/timeout), and stale tag scanning
  - Tests cover: tag filters, VM operations, metadata management, VM allocation, daemon behavior, tag cleaning, SSH checking, event monitoring, and VM tracking

### Run Tests

```bash
# All tests
sudo nix develop -c python3 -m pytest tests/ -v

# VM Manager tests only
sudo nix develop -c python3 -m pytest tests/vm_manager/ -v

# With coverage
sudo nix develop -c python3 -m pytest tests/vm_manager/ --cov=vm_manager --cov-report=html
```

For detailed testing documentation, see [TESTING.md](TESTING.md).

---

## Troubleshooting

### VM Manager won't start

**Symptom**: Error connecting to libvirt

**Solution**:
```bash
# Check libvirtd is running
sudo systemctl status libvirtd

# Verify connection
sudo virsh list

# Run with correct permissions
sudo nix develop -c vm-manager ...
```

### SSH checks always fail

**Symptom**: "SSH authentication failed" messages

**Possible causes**:
1. **Wrong SSH key**: Verify `--ssh-key` path is correct
2. **Wrong username**: Check `--ssh-username` matches VM configuration
3. **Key permissions**: SSH keys should be mode 600
4. **VM not accepting connections**: Verify SSH is enabled in VM

**Debug**:
```bash
# Test SSH manually
ssh -i /path/to/key user@vm-ip

# Check SSH is listening
nc -zv vm-ip 22

# Enable debug logging
vm-manager ... --log-level debug
```

### Tags not being removed

**Symptom**: VMs become SSH-accessible but tags remain

**Possible causes**:
1. **SSH succeeds but tag removal fails**: Check libvirt permissions
2. **VM filters don't match**: Verify `--tag` and `--exclude-tag` settings
3. **Wrong tag specified**: Check `--mark-as-used` value

**Debug**:
```bash
# Check VM tags
sudo virsh desc vm-name

# Enable debug logging
vm-manager ... --log-level debug

# Verify tag matching
# (check logs for "VM matches filters" message)
```

### VMs tagged as `broken`

**Symptom**: VMs have both `used` and `broken` tags

**Cause**: SSH could not connect within `--max-wait-time` (default: 30 minutes). The VM may be running a PXE installer, have a broken OS, or lack SSH configuration.

**Solutions**:
1. **Investigate the VM**: Check console via `virt-manager` or `virsh console`
2. **Remove broken tag**: `sudo virsh desc vm-name --title "" --config` (edit description)
3. **Increase timeout**: `--max-wait-time 3600` for VMs with long install cycles
4. **Disable broken tagging**: `--no-broken-tag` (monitors just stop, no tag added)

### Tags removed while deployer is still running

**Symptom**: VM shows `in_use=true` but no `used` tag

**Cause**: This was a known race condition that has been fixed. If you see this, ensure you are running the latest version. The fix checks `in_use` metadata before removing tags.

### VMs not booting with --boot-always

**Symptom**: Daemon runs but VMs don't boot

**Possible causes**:
1. **Tags removed**: Boot modes only boot VMs with matching tags
2. **Wrong filters**: Check `--tag` matches VM metadata
3. **VMs already running**: Shutdown and wait for daemon to boot them

**Verify**:
```bash
# Check which VMs match filters
sudo virsh list --all | grep vm-prefix

# Check VM tags
sudo virsh desc vm-name

# Test manually
sudo virsh start vm-name
```

### High memory usage

**Symptom**: Daemon uses excessive memory

**Possible causes**:
1. **Many concurrent VMs**: Each monitored VM has a task
2. **Long-running SSH checks**: Reduce `--max-wait-time`
3. **Debug logging accumulation**: Use `info` level for production

**Solutions**:
```bash
# Monitor resource usage
top -p $(pgrep -f vm_manager)

# Reduce max wait time
vm-manager ... --max-wait-time 300

# Use info logging
vm-manager ... --log-level info
```

---

## Security Considerations

### SSH Keys

- Store SSH keys with restricted permissions (mode 600)
- Use dedicated keys for automation (not personal keys)
- Rotate keys regularly
- Consider using SSH agent forwarding in development

### Password Files

If using `--ssh-password-file`:
- File should be mode 600 (readable only by owner)
- Store in secure location (not in /tmp)
- Consider using SSH keys instead

### Libvirt Access

- VM Manager requires system-level libvirt access
- Run with `sudo` or add user to `libvirt` group
- Audit VM tag modifications via libvirt logs

---

## Performance

### Resource Usage

- **CPU**: Low (~1-2% idle, spikes during event processing)
- **Memory**: ~40-60 MB baseline + ~5 MB per monitored VM
- **Network**: Minimal (SSH checks only)

### Scalability

- **Tested**: 30+ concurrent VMs
- **Recommended**: <100 concurrent monitoring sessions
- **Bottlenecks**: SSH connection timeouts, libvirt event processing

### Optimization Tips

1. **Reduce check interval** for faster detection (default: 10s)
2. **Set max wait time** to prevent indefinite waits
3. **Use --check-existing** sparingly (scans all running VMs)
4. **Batch VM operations** instead of starting many VMs at once

---

## Comparison with Alternatives

| Feature | VM Manager | cloud-init | Custom Scripts |
|---------|------------|------------|----------------|
| Event-driven | ✅ Yes | ❌ No | ⚠️ Manual |
| SSH verification | ✅ Full auth | ❌ No | ⚠️ Varies |
| Tag management | ✅ Built-in | ❌ No | ⚠️ Manual |
| Retry logic | ✅ Intelligent | ⚠️ Basic | ⚠️ Manual |
| Boot modes | ✅ Yes | ❌ No | ⚠️ Manual |
| Test coverage | ✅ 100% | N/A | ⚠️ Varies |
| Debouncing | ✅ Yes | ❌ No | ❌ No |

---

## Contributing

This tool is part of the `ansible-vm-deployer` project. See the main [CONTRIBUTING.md](../../CONTRIBUTING.md) for guidelines.

### Running Tests

Before submitting changes:

```bash
# Run all tests
sudo nix develop -c python3 -m pytest tests/vm_manager/ -v

# Check code style
sudo nix develop -c python3 -m ruff check src/vm_manager/

# Format code
sudo nix develop -c python3 -m black src/vm_manager/
```

---

## License

See [LICENSE](../../LICENSE) file in the repository root.

---

## Support

For issues, questions, or contributions, see the main repository:
https://github.com/dvaerum/ansible-vm-deployer

---

## Related Documentation

- [Architecture](ARCHITECTURE.md) - Detailed design and component documentation
- [Testing](TESTING.md) - Test suite documentation and manual testing guides
- [ansible-deployer](../ansible-deployer/README.md) - Complementary tool for Ansible deployment
