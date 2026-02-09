# Usage Guide

Complete guide for using ansible-deployer to deploy Ansible playbooks to libvirt-managed VMs.

## Table of Contents

1. [Installation](#installation)
2. [Configuration](#configuration)
3. [Preparing VMs](#preparing-vms)
4. [Deploying Playbooks](#deploying-playbooks)
5. [Managing VMs](#managing-vms)
6. [Viewing Logs](#viewing-logs)
7. [Troubleshooting](#troubleshooting)

## Installation

### Using Nix (Recommended)

```bash
# Enter development shell
nix develop

# Or build the package
nix build

# Run directly
nix run
```

### Traditional Python Installation

```bash
# Install in development mode
pip install -e .

# Or install from source
pip install .
```

## Configuration

Configuration is primarily done through CLI options. An optional `config.yaml` file can be used for the libvirt connection URI:

```yaml
# config.yaml (optional - defaults work without it)
libvirt_uri: "qemu:///system"
```

The config file is searched in order: `./config.yaml`, `./config.yml`, `/etc/ansible-deployer/config.yaml`. You can also specify a custom location:

```bash
ansible-deployer --config /path/to/config.yaml <command>
```

All other settings are CLI options:

```bash
ansible-deployer \
  --log-dir ./logs \                 # Log directory (default: ./logs)
  --log-level INFO \                 # Logging level (default: INFO)
  --project-root ~/my-project \      # Base directory for relative paths
  -v \                               # Shorthand for --log-level DEBUG
  deploy --network my-network ...    # Network for IP discovery (default: first interface)
```

For remote libvirt hosts, set `libvirt_uri` in the config file:

```yaml
libvirt_uri: "qemu+ssh://user@remote-host/system"
```

## Preparing VMs

### Requirements

1. **Running VMs**: VMs must be running and accessible
2. **QEMU Guest Agent**: Required for VM reset functionality
3. **Tags**: VMs must be tagged for selection
4. **SSH Access**: Ansible needs SSH access to the VMs

### Installing QEMU Guest Agent

On the VM (Ubuntu/Debian):

```bash
sudo apt-get update
sudo apt-get install qemu-guest-agent
sudo systemctl start qemu-guest-agent
sudo systemctl enable qemu-guest-agent
```

On the VM (CentOS/RHEL):

```bash
sudo yum install qemu-guest-agent
sudo systemctl start qemu-guest-agent
sudo systemctl enable qemu-guest-agent
```

### Tagging VMs

See [VM_TAGGING.md](VM_TAGGING.md) for detailed instructions.

Quick example:

```bash
virsh edit my-vm

# Add in the XML:
<description>
Development VM for testing
tags: development, test, ubuntu
</description>
```

## Deploying Playbooks

### Basic Deployment

```bash
ansible-deployer deploy \
  --tag test \
  --playbook ./playbooks/setup.yml
```

### Using Project Root

The `--project-root` and `--log-dir` global flags allow you to organize all deployment files in a single directory structure:

```bash
ansible-deployer --project-root /path/to/my-project --log-dir logs \
  deploy \
  --tag test \
  --playbook playbooks/setup.yml \
  --inventory inventory/hosts.ini
```

**Note:** `--project-root` and `--log-dir` are **global options** that must come **before** the subcommand (`deploy`, `list-logs`, etc.).

**Path Resolution Rules:**
- **Relative paths** (e.g., `playbooks/setup.yml`) are resolved relative to `--project-root`
- **Absolute paths** (e.g., `/etc/ansible/playbook.yml`) are used as-is
- **Wrapper script** is automatically found at `<project-root>/ansible-wrapper.sh`
- **Log directory** defaults to `<project-root>/logs` if not specified

**Example Project Structure:**
```
/path/to/my-project/
├── ansible-wrapper.sh      # Custom wrapper script
├── playbooks/
│   ├── setup.yml
│   └── deploy.yml
├── inventory/
│   ├── production.ini
│   └── staging.ini
└── logs/                   # Created automatically
    ├── task_xxx_stdout.log
    └── task_xxx_json.log
```

**Without `--project-root`** (default behavior):
- All paths are relative to current working directory
- Wrapper script auto-detected from Python module location or uses `ansible-playbook` directly
- Log directory uses value from `config.yaml` or `./logs`

### With Multiple Tags

The tool will match VMs with ANY of the specified tags:

```bash
ansible-deployer deploy \
  --tag development \
  --tag staging \
  --playbook ./playbooks/setup.yml
```

### Excluding Tags

Exclude VMs that have specific tags (useful to filter out maintenance, broken, etc.):

```bash
ansible-deployer deploy \
  --tag test \
  --exclude-tag maintenance \
  --exclude-tag broken \
  --playbook ./playbooks/setup.yml
```

**Behavior**:
- VM must have at least one tag from `--tag` options
- VM must have NONE of the tags from `--exclude-tag` options

### With Extra Variables

Pass JSON-formatted extra variables:

```bash
ansible-deployer deploy \
  --tag production \
  --playbook ./playbooks/deploy.yml \
  --extra-vars '{"version": "1.2.3", "environment": "prod"}'
```

### With Custom Inventory File

You can optionally provide an Ansible inventory file:

```bash
ansible-deployer deploy \
  --tag test \
  --playbook ./playbooks/setup.yml \
  --inventory ./inventory/production.ini
```

**Note:** The inventory file is passed to ansible-playbook via the `-i` flag. VM environment variables (`VM_IP_1`, `VM_IP_2`, `VM_IP_ALL`) are still available regardless of whether an inventory file is provided.

**Example inventory file** (`inventory/hosts.ini`):
```ini
[webservers]
web1 ansible_host=192.168.1.10
web2 ansible_host=192.168.1.11

[databases]
db1 ansible_host=192.168.1.20

[all:vars]
ansible_user=admin
ansible_ssh_private_key_file=~/.ssh/id_rsa
```

### Skip VM Reset

If you don't want the VM to be reset after deployment:

```bash
ansible-deployer deploy \
  --tag test \
  --playbook ./playbooks/setup.yml \
  --no-reset
```

### Multi-VM Allocation

Allocate multiple VMs for distributed system testing:

```bash
ansible-deployer deploy \
  --tag test \
  --vm-count 3 \
  --playbook ./cluster-setup.yml
```

**Wait/Retry Behavior:**
- By default: Waits **indefinitely** for all requested VMs to become available (checks every 60 seconds)
- With timeout: `--allocation-timeout 300` (timeout in seconds)

**Example with timeout:**
```bash
ansible-deployer deploy \
  --tag production \
  --vm-count 5 \
  --allocation-timeout 600 \
  --playbook ./deploy.yml
```

### Environment Variables Available to Playbooks

The following environment variables are automatically set and available to your Ansible playbooks:

**For single VM (`--vm-count 1` or default):**
- **`VM_IP_1`**: The IP address of the first VM
- **`VM_IP_ALL`**: Comma-separated list of all VM IPs (single IP in this case)

**For multiple VMs (`--vm-count N`):**
- **`VM_IP_1`**, **`VM_IP_2`**, **`VM_IP_3`**, ..., **`VM_IP_N`**: Individual VM IP addresses
- **`VM_IP_ALL`**: Comma-separated list of all VM IPs

**Example playbook using environment variables:**

```yaml
---
- name: Multi-VM Deployment
  hosts: localhost
  gather_facts: no
  
  tasks:
    - name: Show all allocated VMs
      debug:
        msg: "Allocated VMs: {{ lookup('env', 'VM_IP_ALL') }}"
    
    - name: Parse VMs as a list
      set_fact:
        vm_ips: "{{ lookup('env', 'VM_IP_ALL').split(',') }}"
    
    - name: Show individual VMs
      debug:
        msg: |
          VM 1: {{ lookup('env', 'VM_IP_1') }}
          VM 2: {{ lookup('env', 'VM_IP_2') }}
          VM 3: {{ lookup('env', 'VM_IP_3') }}
    
    - name: Configure cluster
      shell: |
        echo "Setting up cluster with VMs: $VM_IP_ALL"
        # Your cluster setup logic here
```

**Note:** Ansible inventory is **NOT** automatically populated. Use environment variables to access VM IPs.

## Ansible Wrapper Script

The tool executes Ansible through a customizable wrapper script (`ansible-wrapper.sh`) located in the project root. This allows you to modify Ansible execution without changing Python code.

### How It Works

When you run a deployment:
1. Python code calls `ansible-wrapper.sh` instead of `ansible-playbook` directly
2. Working directory is set to project root (when `--project-root` is used)
3. All environment variables (including `VM_IP_*`) are passed to the wrapper
4. The wrapper executes `ansible-playbook` with your customizations
5. All Ansible output is captured and logged

**Note:** When using `--project-root`, the wrapper script can reference files using relative paths (e.g., `./scripts/deploy-check.sh`, `./env/production.env`).

### Default Wrapper

The default `ansible-wrapper.sh`:

```bash
#!/usr/bin/env bash
set -e

# Optional: Add custom Ansible flags
# CUSTOM_FLAGS=("--forks" "10")

# Execute ansible-playbook with all arguments
exec ansible-playbook "$@" ${CUSTOM_FLAGS[@]}
```

### Customization Examples

#### Example 1: Add Custom Ansible Flags

```bash
#!/usr/bin/env bash
set -e

# Increase parallelism and set timeout
CUSTOM_FLAGS=(
    "--forks" "20"
    "--timeout" "60"
    "--ssh-common-args" "-o StrictHostKeyChecking=no"
)

exec ansible-playbook "$@" ${CUSTOM_FLAGS[@]}
```

#### Example 2: Log All Deployments

```bash
#!/usr/bin/env bash
set -e

# Log deployment details
LOG_FILE="/var/log/ansible-deployments.log"
echo "$(date): Deploying to $VM_IP_ALL" >> "$LOG_FILE"
echo "  Playbook: $1" >> "$LOG_FILE"

exec ansible-playbook "$@"
```

#### Example 3: Pre-Deployment Validation

```bash
#!/usr/bin/env bash
set -e

# Validate at least one VM is allocated
if [[ -z "$VM_IP_1" ]]; then
    echo "Error: No VMs allocated" >&2
    exit 1
fi

# Check connectivity before running playbook
for ip in $(echo "$VM_IP_ALL" | tr ',' ' '); do
    if ! ping -c 1 -W 2 "$ip" > /dev/null 2>&1; then
        echo "Warning: VM $ip not responding to ping" >&2
    fi
done

exec ansible-playbook "$@"
```

#### Example 4: Environment-Specific Configuration

```bash
#!/usr/bin/env bash
set -e

# Use different Ansible config based on environment
if [[ "$VM_IP_ALL" == *"192.168.1."* ]]; then
    export ANSIBLE_CONFIG="/etc/ansible/production.cfg"
else
    export ANSIBLE_CONFIG="/etc/ansible/staging.cfg"
fi

exec ansible-playbook "$@"
```

#### Example 5: Integration with External Tools

```bash
#!/usr/bin/env bash
set -e

# Send notification to Slack/Teams
function notify_deployment() {
    curl -X POST "$WEBHOOK_URL" \
        -d "{\"text\": \"Deploying to $VM_IP_ALL\"}"
}

notify_deployment

exec ansible-playbook "$@"
```

### Environment Variables in Wrapper

All these variables are automatically available:

**VM-specific:**
- `VM_IP_1`, `VM_IP_2`, `VM_IP_3`, ... - Individual VM IPs
- `VM_IP_ALL` - Comma-separated list (e.g., `192.168.1.100,192.168.1.101`)

**Ansible-specific:**
- `ANSIBLE_STDOUT_CALLBACK=json` - Set by the tool
- `ANSIBLE_JSON_INDENT=2` - Set by the tool
- `ANSIBLE_LOAD_CALLBACK_PLUGINS=1` - Set by the tool

**System:**
- All standard environment variables from your shell

### Testing the Wrapper

Test your wrapper script manually:

```bash
# Test with environment variables
VM_IP_1=192.168.1.100 \
VM_IP_ALL=192.168.1.100 \
./ansible-wrapper.sh --version

# Test with a playbook
VM_IP_1=192.168.1.100 \
VM_IP_ALL=192.168.1.100 \
./ansible-wrapper.sh playbooks/test.yml --check
```

### Deployment Process

When you run a deployment, the tool:

1. **Atomically allocates** N available VMs with matching tags (and without excluded tags)
   - Each candidate VM is claimed by writing a task_id, then re-read to verify ownership
   - If another parallel instance claimed the VM first, it is skipped
   - If fewer VMs were claimed than needed, partial claims are released and retried
   - Waits and retries every 60 seconds if not enough VMs available
   - Times out after `--allocation-timeout` seconds (if specified)
2. Gets each VM's IP address
3. Sets environment variables (`VM_IP_1`, `VM_IP_2`, ..., `VM_IP_ALL`)
4. Executes the Ansible playbook (without automatic inventory)
5. Saves both stdout and JSON logs
6. Resets all VMs (wipefs + reboot) unless `--no-reset` is specified
7. Marks all VMs as available

## Monitoring Deployment Progress

When you start a deployment, the tool displays the log file path for real-time monitoring:

```
Executing playbook...
Monitor progress: tail -f /path/to/logs/20240209_153000_abc123_stdout.log
```

### Real-Time Monitoring

The **stdout log file** is written in real-time as Ansible executes. You can monitor progress:

```bash
# In another terminal, watch the stdout log
tail -f ~/project/logs/20240209_153000_abc123_stdout.log
```

### Log Files Generated

Each deployment creates two log files:

1. **`<task_id>_stdout.log`** - Ansible output (written in REAL-TIME)
   - Updated continuously during execution, line by line
   - Use `tail -f` to monitor progress
   - Human-readable Ansible output with colors and formatting
   - **This is the file to watch for monitoring progress!**

2. **`<task_id>_json.log`** - Metadata (written at the end)
   - Created after playbook completes
   - Contains basic task metadata
   - Not for real-time monitoring

**Tip:** If you don't see progress, the deployment might be stuck. Check:
- VM connectivity (SSH access)
- Ansible inventory configuration
- Network issues
- Task timeouts

## Managing VMs

### List All VMs

```bash
ansible-deployer list-vms
```

Output:
```
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━┓
┃ Name        ┃ UUID              ┃ State   ┃ Tags              ┃ In Use  ┃ Task ID   ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━┩
│ test-vm-01  │ xxx-xxx-xxx       │ running │ linux-test, test │ False   │           │
│ test-vm-02  │ yyy-yyy-yyy       │ running │ linux-test, test │ True    │ 20240206_ │
└─────────────┴───────────────────┴─────────┴───────────────────┴─────────┴───────────┘
```

**JSON output:**

```bash
ansible-deployer list-vms --json
```

```json
[
  {
    "name": "test-vm-01",
    "uuid": "xxx-xxx-xxx",
    "state": "running",
    "tags": ["linux-test", "test"],
    "in_use": false,
    "task_id": ""
  }
]
```

JSON output uses proper types (tags as arrays, in_use as booleans) for easy parsing with `jq` or other tools:

```bash
# List only VMs that are in use
ansible-deployer list-vms --json | jq '.[] | select(.in_use == true)'

# Get names of VMs with a specific tag
ansible-deployer list-vms --json | jq -r '.[] | select(.tags[] == "linux-test") | .name'
```

### Show VM Status

```bash
ansible-deployer status --vm-name test-vm-01
```

Output:
```
VM: test-vm-01
UUID: xxx-xxx-xxx-xxx
State: running
In Use: False

Metadata:
  in_use: false
  task_id: 
  finished_at: 2024-02-06T15:30:00

Tags: test, development, ubuntu

Networks:
  default: 192.168.122.100

Interfaces:
  lo: 127.0.0.1
  enp1s0: 192.168.122.100

Default IP (first interface): 192.168.122.100
```

**JSON output:**

```bash
ansible-deployer status --vm-name test-vm-01 --json
```

```json
{
  "name": "test-vm-01",
  "uuid": "xxx-xxx-xxx-xxx",
  "state": "running",
  "in_use": false,
  "task_id": "",
  "tags": ["test", "development", "ubuntu"],
  "metadata": {
    "in_use": "false",
    "task_id": "",
    "finished_at": "2024-02-06T15:30:00"
  },
  "networks": {
    "default": ["192.168.122.100"]
  },
  "interfaces": {
    "lo": ["127.0.0.1"],
    "enp1s0": ["192.168.122.100"]
  },
  "default_ip": "192.168.122.100"
}
```

### Manually Reset a VM

```bash
ansible-deployer reset-vm --vm-name test-vm-01
```

This will:
1. Run `wipefs -af /dev/vda` inside the VM
2. Reboot the VM
3. Mark it as available

## Viewing Logs

Log files are stored in the log directory (default: `./logs/` or `<project-root>/logs/`). Use standard tools to browse and view them:

```bash
# List all logs
ls -lt logs/

# View a specific log
less logs/20240206_153000_abc123_stdout.log

# Search across logs
grep -l "FAILED" logs/*_stdout.log
```

### Organizing Logs with Prefixes

Add a prefix to log filenames for better organization:

```bash
# Production deployment
ansible-deployer deploy \
  --tag production \
  --playbook ./deploy.yml \
  --log-prefix prod-deploy

# Results in:
# logs/prod-deploy_20260210_153000_abc123_stdout.log
# logs/prod-deploy_20260210_153000_abc123_json.log
```

**Subdirectory support**: Use `/` in the prefix to organize logs into subdirectories:

```bash
# Organize by OS
ansible-deployer deploy \
  --tag test \
  --playbook ./test.yml \
  --log-prefix test/linux

# Results in:
# logs/test/linux_20260210_153000_abc123_stdout.log

# Deeper nesting
ansible-deployer deploy \
  --tag test \
  --playbook ./test.yml \
  --log-prefix ci/nightly/linux-9

# Results in:
# logs/ci/nightly/linux-9_20260210_153000_abc123_stdout.log
```

Subdirectories are created automatically. Special characters (spaces, dots, etc.) are replaced with hyphens; only alphanumeric characters, `-`, `_`, and `/` are allowed.

**Use cases:**
- **Environment**: `--log-prefix prod`, `--log-prefix staging`
- **Deployment type**: `--log-prefix hotfix`, `--log-prefix rollback`
- **Application**: `--log-prefix api-deploy`, `--log-prefix web-deploy`
- **Subdirectory organization**: `--log-prefix test/linux`, `--log-prefix ci/nightly/debian`
- **Date-based**: `--log-prefix nightly-$(date +%Y%m%d)`

**Benefits:**
- Easier to find related logs
- Group deployments by type or environment
- Subdirectories keep log directories clean at scale
- Simplifies log analysis and cleanup
- Better organization for CI/CD pipelines

### Repeat Playbook Execution

Run the playbook multiple times on the same VM (e.g., for stress testing or idempotency validation):

```bash
# Run the playbook 5 times, stop on first failure
ansible-deployer deploy \
  --tag test \
  --playbook ./test.yml \
  --repeat 5

# Combine with --log-prefix for organized repeat logs
ansible-deployer deploy \
  --tag test \
  --playbook ./test.yml \
  --repeat 3 \
  --log-prefix stress-test/linux
```

When `--repeat` is greater than 1, each iteration gets its own log files with a `_runN` suffix:
- `stress-test/linux_20260213_120000_abc123_run-1_stdout.log`
- `stress-test/linux_20260213_120000_abc123_run-2_stdout.log`
- `stress-test/linux_20260213_120000_abc123_run-3_stdout.log`

With `--repeat 1` (default), no suffix is added — fully backward compatible.

### Log Files

Logs are stored in the configured log directory (default: `./logs/`):

- `{task_id}_stdout.log`: Standard Ansible output
- `{task_id}_json.log`: JSON-formatted structured log

## Troubleshooting

### No Available VMs

**Problem**: `No available VM found with tags: ...`

**Solutions**:
- Ensure VMs are running: `virsh list --all`
- Check VM tags: `ansible-deployer status --vm-name <vm>`
- Verify VMs aren't stuck "in use": `ansible-deployer list-vms`

### VM IP Not Found

**Problem**: `Could not determine IP address for <vm>`

**Solutions**:
- Ensure QEMU guest agent is running on the VM
- Check DHCP lease: `virsh net-dhcp-leases default`
- Verify network configuration in VM

### SSH Connection Failed

**Problem**: Ansible cannot connect to VM

**Solutions**:
- Verify SSH key is configured for the VM
- Check SSH service is running on VM
- Test manual connection: `ssh <vm-ip>`
- Check firewall rules

### VM Reset Failed

**Problem**: VM reset is skipped with message "guest-exec command is disabled"

This is the expected behavior when the QEMU guest agent has command execution disabled (default on RHEL/CentOS for security).

**To Enable Full VM Reset (Optional)**:

VM reset requires the `guest-exec` command to be enabled in the QEMU guest agent. By default, this is **disabled for security** on many distributions.

**Steps to enable (on each VM)**:

1. SSH into the VM
2. Edit guest agent configuration:
   - RHEL/CentOS: `/etc/sysconfig/qemu-ga`
   - Debian/Ubuntu: `/etc/default/qemu-guest-agent`
3. Remove `guest-exec` from blacklist or set empty blacklist:
   ```bash
   # Clear the blacklist entirely
   BLACKLIST=
   ```
4. Restart the guest agent:
   ```bash
   sudo systemctl restart qemu-guest-agent
   ```

**Security Note**: Enabling `guest-exec` allows the hypervisor to execute arbitrary commands in the VM. Only enable this on:
- Test/development VMs
- VMs in trusted environments  
- VMs where you trust the hypervisor administrator

**Alternative**: Use `--no-reset` flag and manage VM cleanup manually via SSH or snapshots.

### Authentication/Permission Errors

**Problem**: `Libvirt authentication failed` or `authentication unavailable: no polkit agent available`

This occurs when running without sufficient privileges to access libvirt.

**Error Message Example**:
```
Error: Libvirt authentication failed. This tool requires system-level access to libvirt.

Solutions:
  1. Run with sudo: sudo ansible-deployer ...
  2. Add your user to the 'libvirt' group: sudo usermod -aG libvirt $USER
     (Then log out and back in for group changes to take effect)
  3. Configure polkit to allow your user access to libvirt
```

**Solution 1: Run with sudo (Quick/Temporary)**

```bash
sudo ansible-deployer deploy --tag test --playbook setup.yml
```

**Advantages**: Works immediately, no configuration needed
**Disadvantages**: Requires sudo for every command

**Solution 2: Add User to libvirt Group (Recommended)**

```bash
# Add your user to the libvirt group
sudo usermod -aG libvirt $USER

# Log out and log back in, then verify
groups
# Should show: ... libvirt ...

# Test without sudo
ansible-deployer list-vms
```

**Advantages**: Permanent solution, no sudo needed  
**Disadvantages**: Requires logout/login, grants full libvirt access to user

**Solution 3: Configure Polkit (Advanced)**

Create a polkit rule to allow your user access to libvirt:

```bash
sudo tee /etc/polkit-1/rules.d/50-libvirt-user.rules << 'EOF'
polkit.addRule(function(action, subject) {
    if (action.id == "org.libvirt.unix.manage" &&
        subject.user == "YOUR_USERNAME") {
        return polkit.Result.YES;
    }
});
EOF

# Restart polkit
sudo systemctl restart polkit
```

Replace `YOUR_USERNAME` with your actual username.

**Advantages**: Fine-grained access control  
**Disadvantages**: More complex, requires polkit knowledge

**Verification**:

After applying any solution, test with:
```bash
# Without sudo (should work after Solution 2 or 3)
ansible-deployer list-vms

# Should show your VMs, not an authentication error
```

### Connection Refused

**Problem**: `Failed to connect to libvirt` or `connection refused`

**Solutions**:
- Check libvirtd service is running: `sudo systemctl status libvirtd`
- Start libvirtd if stopped: `sudo systemctl start libvirtd`
- Enable libvirtd to start on boot: `sudo systemctl enable libvirtd`
- Verify correct URI in config.yaml (default: `qemu:///system`)
- Check firewall isn't blocking connection (if using remote URI)

## Advanced Usage

### Using with CI/CD

Example GitLab CI configuration:

```yaml
deploy:
  stage: deploy
  script:
    - nix develop --command ansible-deployer deploy \
        --tag ci-runner \
        --playbook ./playbooks/ci-deploy.yml \
        --extra-vars '{"commit": "$CI_COMMIT_SHA"}'
  artifacts:
    paths:
      - logs/
    when: always
```

### Custom Libvirt URI

For remote libvirt hosts:

```bash
ansible-deployer --config custom.yaml deploy \
  --tag production \
  --playbook ./deploy.yml
```

In `custom.yaml`:
```yaml
libvirt_uri: "qemu+ssh://user@remote-host/system"
```

### Parallel Deployments

Run multiple deployments in parallel (to different VMs):

```bash
# Terminal 1
ansible-deployer deploy --tag web --playbook ./web.yml

# Terminal 2
ansible-deployer deploy --tag db --playbook ./db.yml
```

Each will select a different available VM with matching tags.

**Race condition safety**: VM allocation uses an atomic claim-and-verify approach, tested with 15 simultaneous processes. When multiple instances run simultaneously:
1. Both scan for available VMs
2. Each claims a VM by writing all metadata fields (in_use, task_id, started_at) in a **single atomic** `setMetadata()` call
3. Each waits 150ms for any concurrent writers to finish
4. Each re-reads the metadata to verify it still owns the VM (last-writer-wins)
5. If another instance overwrote the claim, the VM is skipped and the next candidate is tried

This prevents two instances from allocating the same VM. See [TECHNICAL_NOTES.md](TECHNICAL_NOTES.md#concurrent-vm-allocation) for the full implementation details and test results.

### Passing Custom Ansible Flags

Use `--ansible-flags` to pass arbitrary flags to `ansible-playbook`:

```bash
# Run in check mode
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  --ansible-flags "--check"

# Check mode with diff
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  --ansible-flags "--check --diff"

# High verbosity for debugging
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  --ansible-flags "-vvv"

# Performance tuning
ansible-deployer deploy \
  --tag production \
  --playbook ./deploy.yml \
  --ansible-flags "--forks 20 --timeout 120"

# Skip specific tags
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  --ansible-flags "--skip-tags debug,test"

# Combine multiple flags
ansible-deployer deploy \
  --tag production \
  --playbook ./deploy.yml \
  --ansible-flags "-vv --check --diff --forks 20"
```

**Note:** The `--ansible-flags` string is parsed using shell-like syntax, so you can use quotes for complex values:
```bash
--ansible-flags "--extra-vars 'complex_var=\"value with spaces\"'"
```

### Quiet Mode

Suppress Ansible output to the console while still writing to log files:

```bash
# Basic quiet mode
ansible-deployer deploy \
  --tag production \
  --playbook deploy.yml \
  --quiet
```

**When to use `--quiet`:**
- **CI/CD pipelines**: Clean output showing only deployment status
- **Background deployments**: Run deployment in background, monitor via logs
- **Parallel deployments**: Multiple deployments without cluttered output
- **Scheduled tasks**: Cron jobs where you don't need console output

**What happens in quiet mode:**
- ✅ Deployment status messages still displayed
- ✅ Log files written in real-time (`tail -f` still works)
- ✅ Errors and warnings still shown
- ❌ Ansible task output NOT printed to console

**Example: Background deployment with monitoring**
```bash
# Terminal 1: Start quiet deployment in background
ansible-deployer deploy \
  --tag production \
  --playbook deploy.yml \
  --quiet &

# Terminal 2: Monitor via log file
tail -f ~/logs/*_stdout.log
```

**Example: CI/CD pipeline**
```yaml
# GitLab CI
deploy:
  script:
    - ansible-deployer deploy \
        --tag production \
        --playbook deploy.yml \
        --quiet
    - echo "Deployment completed with exit code $?"
```

### VM Usage Tagging

Mark VMs with usage tags to track their purpose and prevent accidental reuse:

```bash
# Add "used" tag to VM description (default)
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  --mark-in-use

# Add custom tag to VM description
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  --mark-in-use=production-test

# Add tag and remove it after reset
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  --mark-in-use=testing \
  --mark-available
```

**How it works:**
1. **`--mark-in-use[=TAG]`**: Adds a tag to the VM's description field after allocation
   - Default tag: `used`
   - Custom tag: Use `--mark-in-use=TAG` to specify a different tag
   - Tag is stored in VM XML: `<description>tags: ready, used</description>`

2. **`--mark-available`**: Removes the usage tag after VM reset
   - Only works with `--mark-in-use` (removes the tag that was added)
   - Without this flag, the tag persists across resets

**Use cases:**

**Reserve VMs for specific purposes:**
```bash
# Reserve a VM for long-running tests
ansible-deployer deploy \
  --tag test \
  --exclude-tag reserved \
  --playbook ./long-test.yml \
  --mark-in-use=reserved \
  --no-reset
```

**Temporary marking:**
```bash
# Mark VM as "debugging", clean up after
ansible-deployer deploy \
  --tag test \
  --playbook ./debug.yml \
  --mark-in-use=debugging \
  --mark-available
```

**Exclude previously used VMs:**
```bash
# First deployment: marks VM with "used"
ansible-deployer deploy \
  --tag test \
  --exclude-tag used \
  --playbook ./test1.yml \
  --mark-in-use

# Second deployment: skips VMs with "used" tag
ansible-deployer deploy \
  --tag test \
  --exclude-tag used \
  --playbook ./test2.yml \
  --mark-in-use
```

**Integration with existing tagging:**
- Works with `--tag` and `--exclude-tag` VM selection
- Tag is added to existing tags in description (comma-separated)
- Example: `tags: test, ready` becomes `tags: test, ready, used`

### Passthrough Arguments

Pass additional arguments directly to the wrapper script or `ansible-playbook` command using the `--` separator:

```bash
# Basic syntax
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  -- <additional arguments>
```

**Examples:**

**Ansible syntax check:**
```bash
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  -- --syntax-check
```

**Check mode with diff:**
```bash
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  -- --check --diff
```

**Limit execution to specific hosts:**
```bash
ansible-deployer deploy \
  --tag production \
  --playbook ./deploy.yml \
  --inventory hosts.ini \
  -- --limit webservers
```

**Custom Ansible options:**
```bash
ansible-deployer deploy \
  --tag test \
  --playbook ./setup.yml \
  -- --step --start-at-task "Install packages"
```

**Multiple passthrough arguments:**
```bash
ansible-deployer deploy \
  --tag production \
  --playbook ./deploy.yml \
  -- --tags "setup,deploy" --skip-tags "debug" --limit "app-*"
```

**How it works:**
1. Arguments after `--` are captured as-is (no parsing or validation)
2. Appended to the end of the wrapper script or `ansible-playbook` command
3. Wrapper script receives: `wrapper.sh playbook.yml -i inventory.cfg <passthrough args>`
4. Direct execution receives: `ansible-playbook playbook.yml -i inventory.cfg <passthrough args>`

**Difference from `--ansible-flags`:**

| Feature | `--ansible-flags` | Passthrough (`--`) |
|---------|-------------------|-------------------|
| **Syntax** | Single string, shell-parsed | Multiple arguments, no parsing |
| **Example** | `--ansible-flags "-vvv --check"` | `-- -vvv --check` |
| **Use case** | Frequently used flags | One-off or complex arguments |
| **Quoting** | Requires careful escaping | Natural shell quoting |
| **Position** | After known options | Must be last |

**When to use passthrough:**
- Testing playbooks with `--syntax-check` or `--check`
- One-off flags that you don't want to add to wrapper script
- Complex arguments with spaces or special characters
- Flags not supported by `--ansible-flags` (rare)

**When to use `--ansible-flags`:**
- Flags you use regularly
- Simple, frequently-used options
- When you want the flags documented in the command

**Note:** If `ansible-playbook` doesn't recognize a passthrough argument, it will fail with an error (as expected).

## Complete CLI Reference

### Global Options (before subcommand)

| Flag | Type | Description |
|------|------|-------------|
| `--config <path>` | Path | Config file location (default: searches for config.yaml) |
| `--project-root <dir>` | Path | Base directory for resolving relative paths |
| `--log-dir <path>` | Path | Log directory (relative to project-root if set, default: ./logs) |
| `--log-level <level>` | Choice | Logging level: `debug`, `info`, `warning`, `error` (default: info) |
| `--verbose` / `-v` | Flag | Shorthand for `--log-level debug` |

### Deploy Command Options

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--tag <tag>` | String (multiple) | Yes | VM tag(s) to match (VM needs at least one) |
| `--exclude-tag <tag>` | String (multiple) | No | VM tag(s) to exclude (VM must have none) |
| `--playbook <path>` | Path | Yes | Ansible playbook path |
| `--inventory <path>` | Path | No | Ansible inventory file path |
| `--extra-vars <json>` | JSON String | No | Extra variables for Ansible |
| `--network <name>` | String | No | Libvirt network name for IP discovery |
| `--vm-count <n>` | Integer | No | Number of VMs to allocate (default: 1) |
| `--allocation-timeout <sec>` | Integer | No | VM allocation timeout (default: infinite) |
| `--ansible-flags <flags>` | String | No | Additional ansible-playbook flags (e.g., '-vvv --check') |
| `--log-prefix <prefix>` | String | No | Prefix for log filenames. Supports subdirectories (e.g., 'test/linux') |
| `--repeat <n>` | Integer | No | Number of times to execute the playbook (default: 1). Stops on first failure. |
| `--quiet` | Flag | No | Suppress Ansible output to console (still writes to log files) |
| `--no-reset` | Flag | No | Skip VM reset after execution |
| `--mark-in-use[=TAG]` | String/Flag | No | Add usage tag to VM description (default: 'used') |
| `--mark-available` | Flag | No | Remove usage tag from VM after reset |
| `-- <args>...` | Arguments | No | Pass additional arguments to wrapper/ansible-playbook |

### Other Commands

**`list-vms`** - List all VMs and their status

| Flag | Type | Description |
|------|------|-------------|
| `--json` | Flag | Output in JSON format (arrays for tags, booleans for in_use) |

**`status --vm-name <name>`** - Show detailed VM information

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--vm-name <name>` | String | Yes | VM name |
| `--json` | Flag | No | Output in JSON format (includes metadata, networks, interfaces) |

**`reset-vm --vm-name <name>`** - Manually reset a VM

| Flag | Type | Required | Description |
|------|------|----------|-------------|
| `--vm-name <name>` | String | Yes | VM name to reset |

## Best Practices

1. **Use specific tags**: Tag VMs clearly for their intended purpose
2. **Keep playbooks idempotent**: VMs are reset after each run
3. **Monitor logs**: Check both stdout and JSON logs for detailed information
4. **Clean up regularly**: Remove old log files periodically
5. **Test playbooks**: Use `--tag test` VMs before deploying to production-tagged VMs
6. **Handle failures gracefully**: VMs are always reset and marked available, even on failure
7. **Use --ansible-flags for testing**: Test playbooks with `--check` before actual deployment
8. **Debug with verbosity**: Use `--ansible-flags '-vvv'` to debug specific deployments
9. **VM reset is non-blocking**: Reset completes immediately; VMs reboot in background