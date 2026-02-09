# Network Selection Guide

## Overview

Ansible Deployer supports selecting which libvirt network to use for IP discovery. This is useful when VMs have multiple network interfaces and you want to deploy to a specific network.

You can select networks by their **libvirt network name** (e.g., `mgmt-network`) which is defined in your VM configuration. This is more stable than using interface names (e.g., `vnet556`) which can change.

## Configuration

### Method 1: Global Configuration (config.yaml)

```yaml
# Set default network for all deployments
network: "mgmt-network"  # or null to use first interface
```

### Method 2: Per-Deployment CLI Flag

```bash
# Override default and use specific network
ansible-deployer deploy --tag test --playbook deploy.yml --network mgmt-network
```

### Method 3: Default Behavior (No Configuration)

If no network/interface is specified (both config and CLI), the tool will:
1. Try ARP table, QEMU guest agent, then DHCP leases
2. Return the **first interface with an IPv4 address**

## Network Names

Network names are defined in your VM's XML configuration and remain stable:

```xml
<interface type='network'>
  <source network='mgmt-network'/>  <!-- This is the network name -->
  <target dev='vnet556'/>                 <!-- This is the interface name (dynamically assigned) -->
</interface>
```

**Why network names:**
- **Stable:** Doesn't change when VM restarts
- **Semantic:** Names like `mgmt-network` describe their purpose
- **Portable:** Same across VM clones

**Note:** Interface names like `vnet556` are dynamically assigned by libvirt and can change when VMs restart, which is why we use network names instead.

## How It Works

### IP Discovery Methods

The tool tries multiple sources in order:
1. **ARP table** (most reliable, no agent needed)
2. **QEMU guest agent** (requires guest agent installed)
3. **DHCP leases** (requires DHCP configuration)

### Selection Priority

```
CLI --network flag  >  config.yaml network  >  first interface with IP
```

## Use Cases

### Multiple Network Interfaces

If your VMs have multiple networks (e.g., management + data networks):

```bash
# Deploy via management network
ansible-deployer deploy --tag web --playbook setup.yml --network mgmt-network

# Or set in config for all deployments
network: "mgmt-network"
```

### Finding Available Networks

Use the `status` command to see all networks and interfaces:

```bash
ansible-deployer status --vm-name my-vm
```

**Output:**
```
VM: my-vm
UUID: xxx-xxx-xxx
State: running

Networks:
  mgmt-network: 192.168.1.102
  data-network: 192.168.2.105

Interfaces:
  vnet556: 192.168.1.102
  vnet557: 192.168.2.105
  enp1s0: 192.168.1.102
  enp2s0: (no IP)

Default IP (first interface): 192.168.1.102
```

## Examples

### Example 1: Check Available Networks

```bash
# Check what networks a VM has
ansible-deployer status --vm-name linux-vm-02
```

**Output shows:**
```
Networks:
  mgmt-network: 192.168.1.102  ← Management network
  data-network: 192.168.2.105     ← Data network

Interfaces:
  vnet556: 192.168.1.102
  vnet557: 192.168.2.105
```

### Example 2: Deploy to Specific Network

```bash
# Deploy via management network
ansible-deployer deploy \
  --tag production \
  --playbook deploy.yml \
  --network mgmt-network

# Output:
# Using network: mgmt-network
# VM IP: 192.168.1.102
#   (from network: mgmt-network)
```

### Example 3: Set Default in Config

```yaml
# config.yaml
libvirt_uri: "qemu:///system"
network: "mgmt-network"  # Always use this network
```

Then deploy without --network flag:

```bash
ansible-deployer deploy --tag web --playbook setup.yml
# Will automatically use mgmt-network
```

### Example 4: Wrong Network Error

```bash
# Try to use non-existent network
ansible-deployer deploy --tag test --playbook test.yml --network nonexistent-network

# Error:
# Error: Could not determine IP address for my-vm on network nonexistent-network
```

## Real-World Scenarios

### Scenario 1: Dual Network Setup

**Infrastructure:**
- Management network: `mgmt-network` (192.168.1.0/24)
- Application network: `data-network` (10.0.0.0/16)

**Solution:**
```yaml
# config.yaml - Use management network for Ansible
network: "mgmt-network"
```

### Scenario 2: Per-Environment Networks

**Development:**
```bash
ansible-deployer deploy --tag dev --playbook app.yml --network dev-network
```

**Production:**
```bash
ansible-deployer deploy --tag prod --playbook app.yml --network prod-network
```

### Scenario 3: Fallback to First Interface

```bash
# No interface specified - uses first one with IP
ansible-deployer deploy --tag test --playbook test.yml
# Automatically finds and uses first available IP
```

## Troubleshooting

### Issue: Wrong IP Selected

**Problem:** Ansible deploys to wrong network

**Solution:** Check available networks:
```bash
ansible-deployer status --vm-name my-vm
```

Then specify the correct one:
```bash
ansible-deployer deploy --tag test --playbook test.yml --network mgmt-network
```

### Issue: "Could not determine IP address on network X"

**Problem:** Specified network doesn't exist or has no IP

**Solution:**
1. Check available networks: `ansible-deployer status --vm-name my-vm`
2. Verify network name is correct (case-sensitive)
3. Ensure the network interface has an IP address assigned
4. Verify the network name matches your VM XML: `virsh dumpxml <vm-name> | grep "source network"`

### Issue: Multiple IPs on Same Interface

**Behavior:** Returns the first IP found

**Solution:** If you need specific IP selection beyond interface level, you can:
1. Use Ansible inventory variables
2. Set up DNS names
3. Configure static IP mapping

## Technical Details

### Network Discovery Code

```python
# Get IP from specific network
vm_ip = vm_manager.get_vm_ip(domain, network="mgmt-network")

# Get IP from first interface with IP (default)
vm_ip = vm_manager.get_vm_ip(domain)

# List all networks and interfaces with their IPs
interfaces = vm_manager.list_vm_interfaces(domain)
# Returns: {
#   "networks": {"mgmt-network": ["192.168.1.102"]},
#   "interfaces": {"vnet556": ["192.168.1.102"], "enp1s0": ["192.168.1.102"], ...}
# }
```

### How Network to Interface Mapping Works

1. Parse VM XML to get network-to-interface mapping:
   ```xml
   <source network='mgmt-network'/>  --> maps to --> <target dev='vnet556'/>
   ```
2. Convert network name to interface name
3. Query interface for IP address

### Order of Operations

1. User runs deploy with `--network mgmt-network`
2. Tool finds VM by tags
3. Tool reads VM XML to map `mgmt-network` → `vnet556`
4. Tool queries libvirt for interfaces (tries ARP, agent, DHCP)
5. Tool filters for the mapped interface (`vnet556`)
6. Tool returns first IPv4 address from that interface
7. Ansible deploys to that IP

## Best Practices

1. **Use Network Names:** Prefer `--network` over `--interface` for stability
2. **Use Status First:** Always check `status` to see available networks
3. **Set Default:** Configure default network in config.yaml for consistency
4. **Document Networks:** Keep a record of which networks are for what purpose
5. **Test First:** Test with `--no-reset` flag when trying new networks
6. **Naming Convention:** Use descriptive network names (e.g., `mgmt-network`, `app-network`)
7. **VM XML:** Check VM XML to verify network names: `virsh dumpxml <vm> | grep "source network"`

## API Reference

### get_vm_ip(domain, network=None)

```python
def get_vm_ip(
    domain: libvirt.virDomain, 
    network: Optional[str] = None
) -> Optional[str]:
    """
    Get IP address of a VM.
    
    Args:
        domain: libvirt domain object
        network: Libvirt network name (e.g., 'mgmt-network')
                If None, returns first IP found.
    
    Returns:
        IP address or None
    """
```

### list_vm_interfaces(domain)

```python
def list_vm_interfaces(domain: libvirt.virDomain) -> Dict[str, Dict[str, List[str]]]:
    """
    List all network interfaces and their IPs.
    
    Args:
        domain: libvirt domain object
    
    Returns:
        Dictionary with 'networks' and 'interfaces' keys
        Example: {
            "networks": {"mgmt-network": ["192.168.1.102"]},
            "interfaces": {"vnet556": ["192.168.1.102"], "vnet557": ["10.0.0.5"]}
        }
    """
```

### get_network_to_interface_mapping(domain)

```python
def get_network_to_interface_mapping(domain: libvirt.virDomain) -> Dict[str, str]:
    """
    Get mapping of libvirt network names to interface names.
    
    Args:
        domain: libvirt domain object
    
    Returns:
        Dictionary mapping network names to interface names
        Example: {"mgmt-network": "vnet556", "data-network": "vnet557"}
    """
```

## Version History

- **v0.2.0+**: Libvirt network name selection (current)
  - `--network` CLI flag for selecting by network name
  - `network` config option for default network
  - `get_network_to_interface_mapping()` method for network-to-interface mapping
  - `list_vm_interfaces()` returns both networks and interfaces
  - `status` command shows both networks and interfaces
  - **Breaking Change:** Removed `--interface` flag and `network_interface` config option

- **v0.1.0**: Initial release
  - Basic VM management features
  - Tag-based VM selection
  - Ansible playbook deployment

---

**See Also:**
- [Usage Guide](USAGE.md)
- [VM Tagging Guide](VM_TAGGING.md)
- [Quick Start](QUICKSTART.md)
