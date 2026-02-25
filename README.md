# VM Management Tools for Libvirt

A comprehensive suite of tools for managing libvirt VMs in automated environments.

## Tools

### 1. Ansible Deployer
Deploy Ansible playbooks to ephemeral VMs with automatic cleanup and comprehensive logging.

**Use cases:**
- CI/CD pipelines
- Testing infrastructure code
- Distributed system testing
- Ephemeral test environments

**Features:**
- Tag-based VM selection with exclusion filters
- Multi-VM allocation for distributed testing
- Automatic VM reset (wipefs + reboot) after deployment
- Comprehensive logging (stdout + JSON)
- Network-based VM selection
- Metadata-based locking to prevent conflicts

### 2. VM Manager
Monitor libvirt VMs and automatically manage tags based on SSH connectivity.

**Use cases:**
- Automated VM provisioning workflows
- CI/CD VM pools with health monitoring
- Development environment management
- Self-service VM allocation

**Features:**
- Event-driven monitoring (libvirt lifecycle events)
- SSH connectivity verification with retry logic
- Automatic tag removal when VMs are ready
- Two-phase broken VM timeout (`--broken-timeout` + `--on-broken-delay`)
- Broken VM detection and tagging (with optional `--on-broken` script hook)
- Auto-exclude broken VMs from monitoring and allocation
- Boot management (start shutdown VMs)
- Debouncing during reboots
- Parallel processing with asyncio

[→ VM Manager Documentation](docs/vm-manager/)

## Overview

### Ansible Deployer
Automates the process of:
1. Selecting one or more available VMs from your libvirt pool based on tags
2. Marking VMs as in-use to prevent conflicts
3. Deploying your Ansible playbook with VM IPs as environment variables
4. Capturing both standard and JSON-formatted logs
5. Automatically resetting VMs (wipefs + reboot) after deployment
6. Marking VMs as available for the next deployment

### VM Manager
Automates VM readiness detection:
1. Monitors libvirt for VM lifecycle events (domain start, reboot)
2. Detects VMs with specific tags (e.g., "provision-me")
3. Verifies SSH connectivity with uptime verification
4. Removes tags when VMs are ready (e.g., remove "provision-me", add "used")
5. Tags VMs as `broken` after configurable timeout (`--broken-timeout`, default 5min)
6. Auto-excludes broken VMs from monitoring and allocation
7. Optionally boots shutdown VMs automatically

## Features

- **Tag-Based VM Selection**: Use flexible tags to match VMs for specific purposes
- **Tag Exclusion**: Exclude VMs with specific tags (e.g., maintenance, broken)
- **Multi-VM Allocation**: Allocate multiple VMs for distributed system testing
- **Wait/Retry Logic**: Automatically waits for VMs to become available (with optional timeout)
- **Network Selection**: Select VMs by libvirt network name for multi-network environments
- **Environment Variables**: VM IPs automatically exported (`VM_IP_1`, `VM_IP_2`, `VM_IP_ALL`)
- **Ansible Wrapper Script**: Customize Ansible execution without modifying Python code
- **Comprehensive Logging**: Capture both stdout and JSON-formatted Ansible logs (single execution)
- **Automatic VM Reset**: Non-blocking wipefs and reboot (completes immediately)
- **Metadata Management**: Track VM usage state in libvirt XML metadata
- **Professional Structure**: Well-organized Python project with type hints
- **Nix Flake Support**: Reproducible development environment
- **Rich CLI**: Beautiful terminal output with tables and colors
- **Concurrent Deployments**: Run multiple deployments to different VMs in parallel

## Quick Start

### Ansible Deployer

See [Quick Start Guide](docs/QUICKSTART.md) for a 5-minute tutorial.

#### Using Nix (Recommended)

```bash
# Initialize flake
nix flake update

# Enter development shell
nix develop

# Deploy a playbook
python -m ansible_deployer deploy --tag test --playbook ./playbooks/example-setup.yml
```

#### Traditional Python Setup

```bash
# Install dependencies
pip install -e .

# Run the application
ansible-deployer deploy --tag test --playbook ./playbooks/example-setup.yml
```

### VM Manager

See [VM Manager Documentation](docs/vm-manager/README.md) for complete usage guide.

#### Using Nix

```bash
# Run vm-manager
sudo nix develop -c python3 -m vm_manager.cli \
  --tag provision-me \
  --ssh-username root \
  --ssh-key ~/.ssh/id_ed25519 \
  --mark-as-used used
```

#### NixOS Service

```nix
{
  services.vm-manager = {
    enable = true;
    tags = [ "provision-me" ];
    ssh = {
      username = "root";
      keyFile = /root/.ssh/vm-manager-key;
    };
    markAsUsed = "used";
    brokenTag = "broken";          # Tag VMs that fail SSH (default)
    # onBroken = /path/to/alert.sh;  # Optional: script to run when VM breaks
  };
}
```

## Documentation

### Ansible Deployer
- [Quick Start Guide](docs/QUICKSTART.md) - Get up and running in 5 minutes
- [Usage Guide](docs/USAGE.md) - Comprehensive usage documentation
- [VM Tagging Guide](docs/VM_TAGGING.md) - How to tag VMs for selection
- [Network Interface Selection](docs/NETWORK_INTERFACE_SELECTION.md) - Network-based VM selection
- [Wrapper Script Examples](docs/WRAPPER_EXAMPLES.md) - Customization examples
- [Technical Notes](docs/TECHNICAL_NOTES.md) - Design decisions and implementation details

### VM Manager
- [User Guide](docs/vm-manager/README.md) - Complete usage guide with CLI reference
- [Architecture](docs/vm-manager/ARCHITECTURE.md) - Technical design and implementation
- [Testing](docs/vm-manager/TESTING.md) - Test suite documentation
- [NixOS Module](nixos-modules/README.md) - Declarative configuration guide

## Usage Examples

### Deploy to a Tagged VM

```bash
ansible-deployer deploy \
  --tag test \
  --playbook ./playbooks/setup.yml
```

### Deploy with Extra Variables

```bash
ansible-deployer deploy \
  --tag production \
  --playbook ./deploy.yml \
  --extra-vars '{"version": "1.2.3", "environment": "prod"}'
```

### Deploy with Custom Inventory

```bash
ansible-deployer deploy \
  --tag test \
  --playbook ./deploy.yml \
  --inventory ./inventory/production.ini
```

### Deploy with Project Root

Use `--project-root` and `--log-dir` global flags to organize all deployment files:

```bash
ansible-deployer --project-root /path/to/my-project --log-dir logs \
  deploy \
  --tag test \
  --playbook playbooks/setup.yml \        # → /path/to/my-project/playbooks/setup.yml
  --inventory inventory/hosts.ini         # → /path/to/my-project/inventory/hosts.ini
  # Logs: /path/to/my-project/logs
  # Wrapper: /path/to/my-project/ansible-wrapper.sh
```

**Global Flags (before subcommand):**
- `--project-root <path>` - Base directory for all relative paths
- `--log-dir <path>` - Log directory (default: `./logs`)

**Behavior:**
- All relative paths are resolved relative to `--project-root`
- Absolute paths (starting with `/`) are used as-is
- Wrapper script is found at `<project-root>/ansible-wrapper.sh`
- If wrapper doesn't exist, falls back to `ansible-playbook` directly

### Deploy with Tag Exclusion

```bash
ansible-deployer deploy \
  --tag test \
  --exclude-tag maintenance \
  --exclude-tag broken \
  --playbook ./setup.yml
```

### Deploy to Specific Network

```bash
ansible-deployer deploy \
  --tag production \
  --network mgmt-network \
  --playbook ./deploy.yml
```

### Deploy to Multiple VMs

```bash
# Allocate 3 VMs for distributed system testing
ansible-deployer deploy \
  --tag test \
  --vm-count 3 \
  --playbook ./cluster-setup.yml

# With allocation timeout (waits max 10 minutes)
ansible-deployer deploy \
  --tag production \
  --vm-count 5 \
  --allocation-timeout 600 \
  --playbook ./deploy.yml
```

### Deploy with Custom Ansible Options

```bash
# Run in check mode with diff output
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  --ansible-flags "--check --diff"

# High verbosity for debugging
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  --ansible-flags "-vvv"

# Combine multiple options
ansible-deployer deploy \
  --tag production \
  --playbook ./deploy.yml \
  --ansible-flags "--forks 20 --timeout 120 -vv"
```

### Organize Logs with Prefixes

```bash
# Add a prefix to log filenames for better organization
ansible-deployer deploy \
  --tag production \
  --playbook ./deploy.yml \
  --log-prefix prod-deploy

# Results in log files:
# logs/prod-deploy_20260210_153000_abc123_stdout.log
# logs/prod-deploy_20260210_153000_abc123_json.log

# Use subdirectories to organize by category:
ansible-deployer deploy \
  --tag test \
  --playbook ./test.yml \
  --log-prefix test/linux

# Results in log files:
# logs/test/linux_20260210_153000_abc123_stdout.log
# logs/test/linux_20260210_153000_abc123_json.log

# Nest deeper for more structure:
ansible-deployer deploy \
  --tag test \
  --playbook ./test.yml \
  --log-prefix ci/nightly/linux-9

# Results in log files:
# logs/ci/nightly/linux-9_20260210_153000_abc123_stdout.log
```

### Repeat Playbook Execution

```bash
# Run the playbook 5 times on the same VM, stop on first failure
ansible-deployer deploy \
  --tag test \
  --playbook ./test.yml \
  --repeat 5
```

### Quiet Mode (Suppress Console Output)

```bash
# Run without printing Ansible output to console (still writes to log files)
ansible-deployer deploy \
  --tag production \
  --playbook ./deploy.yml \
  --quiet

# Useful for:
# - CI/CD pipelines where you only want final status
# - Running multiple deployments in parallel
# - Keeping console clean while monitoring via log files
# - Background deployments

# Combine with log monitoring
ansible-deployer deploy --tag prod --playbook deploy.yml --quiet &
tail -f ~/logs/*_stdout.log
```

### List All VMs and Their Status

```bash
ansible-deployer list-vms
```

### View VM Details

```bash
ansible-deployer status --vm-name test-vm-01
```

### Monitor Deployment Progress

```bash
# The tool shows you the log file path when deployment starts
# Monitor progress in real-time using tail:
tail -f ~/project/logs/20240209_153000_abc123_stdout.log
```

### View Deployment Logs

```bash
# List log files
ls ./logs/

# View a specific log (stdout is human-readable, json is structured)
cat ./logs/20240206_153000_abc123_stdout.log
less ./logs/20240206_153000_abc123_json.log
```

## Ansible Wrapper Script

The tool executes Ansible through a customizable wrapper script (`ansible-wrapper.sh`) in the project root. This allows you to modify Ansible execution without changing Python code.

**Working Directory:** When using `--project-root`, the wrapper script runs with its working directory set to the project root, allowing you to use relative paths for files in your project.

### Arguments Passed to Wrapper

The wrapper script receives these arguments (in order):
1. **Playbook path** - `/path/to/playbook.yml` (always)
2. **Inventory** - `-i /path/to/inventory` (if `--inventory` specified)
3. **Extra vars** - `--extra-vars '{"key":"value"}'` (if `--extra-vars` specified)
4. **Additional flags** - Any flags from `--ansible-flags` (if specified)

**Example:** `ansible-wrapper.sh /path/to/playbook.yml -i /path/to/inventory --extra-vars '{"version":"1.0"}' --check --diff -vvv`

**Note:** Use `--ansible-flags` to pass any ansible-playbook flags including verbosity (`-vvv`), check mode (`--check`), etc.

### Environment Variables Available

All VM IPs are automatically exported as environment variables:
- `VM_IP_1`, `VM_IP_2`, `VM_IP_3`, ... - Individual VM IPs
- `VM_IP_ALL` - Comma-separated list of all VM IPs

### Customization Examples

Edit `ansible-wrapper.sh` to customize execution:

```bash
#!/usr/bin/env bash

# Example 1: Add custom Ansible flags
CUSTOM_FLAGS=("--forks" "10" "--timeout" "30")

# Example 2: Log deployments
echo "$(date): Deploying to $VM_IP_ALL" >> /var/log/deployments.log

# Example 3: Pre-deployment validation
if [[ -z "$VM_IP_1" ]]; then
    echo "Error: No VMs allocated"
    exit 1
fi

# Execute ansible-playbook with all arguments
exec ansible-playbook "$@" ${CUSTOM_FLAGS[@]}
```

### Access IPs in Playbooks

Environment variables are also available inside your Ansible playbooks:

```yaml
- name: Example Playbook
  hosts: localhost
  tasks:
    - name: Show allocated VMs
      debug:
        msg: "Deploying to: {{ lookup('env', 'VM_IP_ALL') }}"
    
    - name: Configure cluster
      shell: ./setup-cluster.sh {{ lookup('env', 'VM_IP_1') }} {{ lookup('env', 'VM_IP_2') }}
```

## Project Structure

```
.
├── src/
│   ├── ansible_deployer/      # Ansible deployment tool
│   │   ├── cli.py             # Command-line interface
│   │   ├── vm_manager.py      # libvirt VM management
│   │   ├── ansible_executor.py # Ansible playbook execution
│   │   ├── vm_reset.py        # VM reset functionality
│   │   ├── metadata_manager.py # VM metadata management
│   │   └── config.py          # Configuration management
│   ├── vm_manager/            # VM monitoring tool
│   │   ├── cli.py             # Command-line interface
│   │   ├── daemon.py          # Main daemon with signal handling
│   │   ├── event_monitor.py  # Libvirt event monitoring
│   │   ├── ssh_checker.py    # SSH connectivity verification
│   │   ├── tag_cleaner.py    # Tag cleanup orchestration
│   │   └── vm_tracker.py     # Session management
│   └── vm_tools_common/       # Shared library
│       ├── vm_operations.py   # Common VM operations
│       ├── libvirt_connection.py # Connection management
│       ├── tag_filters.py     # Tag filtering logic
│       └── exceptions.py      # Common exceptions
├── docs/
│   ├── [ansible_deployer docs] # Ansible Deployer documentation
│   └── vm-manager/            # VM Manager documentation
│       ├── README.md          # User guide
│       ├── ARCHITECTURE.md    # Technical design
│       └── TESTING.md         # Test suite docs
├── nixos-modules/             # NixOS integration
│   ├── vm-manager.nix         # NixOS module
│   ├── example-configuration.nix # Configuration examples
│   └── README.md              # Module documentation
├── tests/                     # Test suite (424 tests)
│   ├── conftest.py            # Shared test fixtures
│   ├── test_*.py              # Common tests (tag filters, VM ops, allocation)
│   ├── ansible_deployer/      # Ansible Deployer tests
│   └── vm_manager/            # VM Manager tests
├── playbooks/                 # Example Ansible playbooks
├── ansible-wrapper.sh         # Customizable Ansible wrapper script
├── config.example.yaml        # Configuration template
├── CHANGELOG.md               # Version history
├── LICENSE                    # MIT License
├── flake.nix                  # Nix flake configuration
├── pyproject.toml             # Python project configuration
└── README.md                  # This file
```

## Configuration

Create `config.yaml` in your project directory (optional — defaults work out of the box):

```yaml
# Simple: single local host (this is the default even without a config file)
libvirt_uri: "qemu:///system"
```

For **multiple libvirt hosts**, use named connections — VMs are searched across all hosts during allocation:

```yaml
# Multi-host: search VMs across multiple libvirt hypervisors
libvirt_connections:
  local:
    uri: "qemu:///system"
  remote-server:
    uri: "qemu+ssh://root@10.0.0.5/system?keyfile=/root/.ssh/id_rsa"
    network: "mgmt-net"  # optional: preferred network for IP resolution on this host
```

Hosts are searched in config order. Unreachable hosts are skipped with a warning. Auth parameters (SSH keys, host verification) are encoded in the URI — see [libvirt URI docs](https://libvirt.org/uri.html).

Config file search order: `./config.yaml` → `./config.yml` → `<project-root>/config.yaml` → `~/.config/ansible-deployer/config.yaml` → `/etc/ansible-deployer/config.yaml`

Other settings are CLI options:
- `--log-dir` — Log directory (default: `./logs`)
- `--log-level` — Log verbosity: `debug`, `info`, `warning`, `error` (default: `info`)
- `--network` — Libvirt network for IP resolution (deploy subcommand)
- `--ansible-flags` — Pass arbitrary flags to ansible-playbook

## Requirements

- Python 3.9+
- libvirt/KVM
- Ansible
- QEMU guest agent installed in VMs (for reset functionality)
  - **Important:** guest-exec command must be enabled in the agent (disabled by default in RHEL/CentOS for security)
  - See troubleshooting section for configuration instructions

## Development

```bash
# Enter Nix development shell
nix develop

# Run tests
sudo nix develop -c python3 -m pytest tests/ -v

# Format code
black src/
ruff check src/

# Type checking
mypy src/
```

### Test Suite

**424 total tests (100% pass rate):**
- Comprehensive coverage of multi-host libvirt connections, all 7 race condition fixes, two-phase broken VM timeout, auto-exclude behavior, `--on-broken` script hook (with retry/timeout/repair flow), CancelledError handling, and stale tag scanning
- Tests cover: tag filters, VM operations, metadata management, VM allocation, daemon behavior, tag cleaning, SSH checking, event monitoring, and VM tracking
- **All tests use mocked dependencies** — no real VMs or libvirt connection needed

See documentation for details:
- [Ansible Deployer Testing](docs/TECHNICAL_NOTES.md#testing)
- [VM Manager Testing](docs/vm-manager/TESTING.md)

## How It Works

1. **VM Selection**: Finds a running VM with matching tags that's not currently in use
2. **Lock VM**: Marks the VM as in-use in its libvirt metadata
3. **Get IP**: Retrieves the VM's IP address via QEMU guest agent
4. **Execute Playbook**: Runs Ansible playbook against the VM
5. **Log Everything**: Saves stdout and JSON logs with unique task ID
6. **Reset VM**: Executes `wipefs -af /dev/vda` and initiates reboot (non-blocking, ~3 seconds)
7. **Release VM**: Marks the VM as available for the next deployment (VM reboots in background)

## Contributing

This project follows professional Python development practices:

- Type hints throughout
- Comprehensive error handling
- Structured logging
- Test coverage
- Documentation

## License

MIT

## Author

Dennis Vestergaard Værum

Created for managing ephemeral test VMs in CI/CD pipelines and automating VM provisioning workflows.