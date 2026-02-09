# VM Tagging Guide

This guide explains how to tag VMs in libvirt so they can be selected by ansible-deployer.

## Overview

The ansible-deployer uses tags stored in the VM's description field to match VMs for playbook deployment. Tags are specified in the format `tags: tag1, tag2, tag3` within the VM's description.

## Tagging a VM

### Method 1: Using virsh

1. Edit the VM's XML description:

```bash
virsh edit <vm-name>
```

2. Add or modify the `<description>` element inside the `<domain>` tag:

```xml
<domain type='kvm'>
  <name>test-vm-01</name>
  <description>
Test VM for Ansible deployments
tags: test-vm, ubuntu, development
  </description>
  <!-- rest of the XML -->
</domain>
```

3. Save and exit. The changes take effect immediately.

### Method 2: Using virt-manager

1. Open virt-manager
2. Right-click on the VM and select "Open"
3. Click on "Show virtual hardware details" (the info icon)
4. Click on "Overview" in the left panel
5. In the "Description" field, add your tags:

```
Test VM for Ansible deployments
tags: test-vm, ubuntu, development
```

6. Click "Apply"

### Method 3: Programmatically with Python

```python
import libvirt

conn = libvirt.open("qemu:///system")
domain = conn.lookupByName("test-vm-01")

# Set description with tags
description = """
Test VM for Ansible deployments
tags: test-vm, ubuntu, development
"""

# This requires editing and redefining the domain XML
xml = domain.XMLDesc(0)
# Modify XML to include description
# Then redefine the domain
```

## Tag Format

- Tags must be on a line starting with `tags:`
- Multiple tags are separated by commas
- Whitespace around tags is automatically trimmed
- Tags are case-sensitive

### Example

```
tags: production, web-server, nginx
tags: staging, database, postgresql  
tags: test-vm
```

## Using Tags

When deploying a playbook, specify one or more tags:

```bash
# Match VMs with the "test-vm" tag
ansible-deployer deploy --tag test-vm --playbook ./playbooks/setup.yml

# Match VMs with either "staging" OR "development" tags
ansible-deployer deploy --tag staging --tag development --playbook ./playbooks/setup.yml
```

The tool will:
1. Find all running VMs
2. Filter for VMs not currently in use
3. Match VMs that have ANY of the specified tags
4. Select the first matching VM

## Recommended Tagging Strategy

Consider using a hierarchical tagging system:

- **Environment**: `production`, `staging`, `development`, `test`
- **Purpose**: `web-server`, `database`, `cache`, `worker`
- **OS**: `ubuntu`, `debian`, `centos`, `alpine`
- **Application**: `wordpress`, `django`, `rails`, `nodejs`

Example:
```
tags: development, web-server, ubuntu, nodejs
```

## Viewing VM Tags

List all VMs and their information:

```bash
ansible-deployer list-vms
```

View detailed information including tags for a specific VM:

```bash
ansible-deployer status --vm-name test-vm-01
```

## Best Practices

1. **Be consistent**: Use a standardized naming convention for tags
2. **Be specific**: Use descriptive tags that clearly indicate the VM's purpose
3. **Document**: Maintain a list of available tags and their meanings
4. **Avoid conflicts**: Don't use the same tag for VMs with different purposes
5. **Update regularly**: Keep tags current as VM purposes change

## Example VM Setup

Here's a complete example of setting up a test VM:

```bash
# Create or edit VM
virsh edit test-vm-01

# Add description with tags
<description>
Ubuntu 22.04 test VM for development
tags: test, ubuntu, development
</description>

# Ensure VM is running
virsh start test-vm-01

# Verify qemu-guest-agent is installed (required for wipefs/reboot)
# Inside the VM:
sudo apt-get install qemu-guest-agent
sudo systemctl start qemu-guest-agent
sudo systemctl enable qemu-guest-agent

# Deploy a playbook
ansible-deployer deploy --tag test --playbook ./playbooks/example-setup.yml
```