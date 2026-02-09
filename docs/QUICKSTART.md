# Quick Start Guide

Get started with ansible-deployer in 5 minutes.

## Prerequisites

- NixOS or Nix package manager installed
- libvirt/KVM set up and running
- At least one VM configured and running

## Step 1: Clone and Setup

```bash
cd ansible-vm-deployer

# Enter Nix development shell
nix develop

# The shell will set up all dependencies automatically
```

## Step 2: Prepare a VM

Tag a VM for testing:

```bash
# Edit VM configuration
virsh edit test-vm-01

# Add tags in the description:
# <description>
# Test VM
# tags: test, development
# </description>
```

Ensure QEMU guest agent is installed in the VM:

```bash
# SSH into your VM
ssh user@vm-ip

# Install guest agent (Ubuntu/Debian)
sudo apt-get install qemu-guest-agent
sudo systemctl start qemu-guest-agent

# Or for CentOS/RHEL
sudo yum install qemu-guest-agent
sudo systemctl start qemu-guest-agent
```

## Step 3: Create a Simple Playbook

Use the example playbook:

```bash
# The example playbook is already at playbooks/example-setup.yml
cat playbooks/example-setup.yml
```

## Step 4: Deploy

```bash
# Deploy the playbook to a VM with the "test" tag
python -m ansible_deployer deploy \
  --tag test \
  --playbook ./playbooks/example-setup.yml
```

Watch the output:
1. VM is selected
2. Playbook executes
3. Logs are saved
4. VM is automatically reset
5. VM is marked as available

## Step 5: View Results

```bash
# List all deployment logs
python -m ansible_deployer list-logs

# View a specific log
python -m ansible_deployer show-log --task-id <task-id>

# Check VM status
python -m ansible_deployer list-vms
```

## Configuration (Optional)

Create `config.yaml` for custom settings:

```bash
cp config.example.yaml config.yaml
# Edit config.yaml as needed
```

## Next Steps

- Read the full [Usage Guide](USAGE.md)
- Learn about [VM Tagging](VM_TAGGING.md)
- Explore the example playbooks in `playbooks/`
- Create your own playbooks for your infrastructure

## Troubleshooting

If you encounter issues:

1. **Verify VM is running**: `virsh list`
2. **Check VM tags**: `python -m ansible_deployer status --vm-name test-vm-01`
3. **Test QEMU agent**: `virsh qemu-agent-command test-vm-01 '{"execute":"guest-ping"}'`
4. **Check logs**: Application logs are in `ansible-deployer.log`

For more help, see the [Usage Guide](USAGE.md#troubleshooting).