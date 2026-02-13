# NixOS Module for VM Manager

This directory contains the NixOS module for declaratively configuring the VM Manager daemon.

## Quick Start

### 1. Add to your flake inputs

```nix
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    vm-tools.url = "github:dvaerum/ansible-vm-deployer";
  };

  outputs = { self, nixpkgs, vm-tools, ... }: {
    nixosConfigurations.your-hostname = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        vm-tools.nixosModules.vm-manager
        ./configuration.nix
      ];
    };
  };
}
```

### 2. Enable in your configuration

```nix
# configuration.nix
{ config, pkgs, ... }:

{
  services.vm-manager = {
    enable = true;
    
    tags = [ "provision-me" ];
    
    ssh = {
      username = "root";
      keyFile = /root/.ssh/vm-manager-key;
    };
  };
}
```

### 3. Rebuild your system

```bash
sudo nixos-rebuild switch --flake .#your-hostname
```

## Configuration Options

### Required Options

#### `services.vm-manager.enable`
- **Type**: `boolean`
- **Default**: `false`
- **Description**: Enable the VM Manager daemon

#### `services.vm-manager.tags`
- **Type**: `list of string`
- **Example**: `[ "provision-me" "ci-test" ]`
- **Description**: List of tags to monitor. VMs must have at least one of these tags.

#### `services.vm-manager.ssh.username`
- **Type**: `string`
- **Example**: `"root"`
- **Description**: SSH username for connectivity checks

#### `services.vm-manager.ssh.keyFile` OR `services.vm-manager.ssh.passwordFile`
- **Type**: `null or path`
- **Default**: `null`
- **Description**: Path to SSH authentication credentials (at least one required)

### Optional Options

#### `services.vm-manager.excludeTags`
- **Type**: `list of string`
- **Default**: `[]`
- **Example**: `[ "production" "manual-only" ]`
- **Description**: VMs with these tags will not be monitored

#### `services.vm-manager.tagToRemove`
- **Type**: `null or string`
- **Default**: `null`
- **Example**: `"used"`
- **Description**: Tag to remove when SSH succeeds (if null, removes monitored tags)

#### `services.vm-manager.bootAtStart`
- **Type**: `boolean`
- **Default**: `false`
- **Description**: Boot matching shutdown VMs once at startup

#### `services.vm-manager.bootAlways`
- **Type**: `boolean`
- **Default**: `false`
- **Description**: Continuously boot matching shutdown VMs

#### `services.vm-manager.checkInterval`
- **Type**: `positive integer`
- **Default**: `10`
- **Description**: Seconds between SSH retry attempts

#### `services.vm-manager.maxWaitTime`
- **Type**: `null or positive integer`
- **Default**: `1800`
- **Example**: `300`
- **Description**: Maximum seconds to wait for SSH. Defaults to 1800 (30 minutes). Set to `null` for infinite (not recommended).

#### `services.vm-manager.brokenTag`
- **Type**: `null or string`
- **Default**: `"broken"`
- **Example**: `"needs-repair"`
- **Description**: Tag to add to VMs that fail SSH after `maxWaitTime`. The `used` tag is kept so the VM won't be reallocated. Set to `null` to disable broken tagging. The daemon automatically excludes VMs with this tag from monitoring, preventing infinite re-monitoring loops.

#### `services.vm-manager.onBroken`
- **Type**: `null or path`
- **Default**: `null`
- **Example**: `/path/to/handler.sh`
- **Description**: Path to an external script to run when a VM is marked broken. The script receives VM information via environment variables: `VM_NAME`, `VM_UUID`, `VM_IP`, `VM_TAGS`, `VM_BROKEN_TAG`, `VM_WAIT_TIME`, `LIBVIRT_URI`. The script runs asynchronously with a 60-second timeout. Non-zero exit codes are logged as warnings but don't affect vm-manager operation.

#### `services.vm-manager.staleScanInterval`
- **Type**: `unsigned integer`
- **Default**: `300`
- **Example**: `600`
- **Description**: Interval in seconds between periodic scans for stale `used` tags. Removes tags from VMs that are no longer actively in use but still have a `used` tag (e.g., because the VM was never rebooted after the deploy finished). Set to `0` to disable.

#### `services.vm-manager.checkExisting`
- **Type**: `boolean`
- **Default**: `false`
- **Description**: Check existing running VMs at startup. Actively in-use VMs go through SSH monitoring; stale tags are removed directly.

#### `services.vm-manager.libvirtUri`
- **Type**: `string`
- **Default**: `"qemu:///system"`
- **Description**: Libvirt connection URI

#### `services.vm-manager.logLevel`
- **Type**: `enum [ "debug" "info" "warning" "error" ]`
- **Default**: `"info"`
- **Description**: Log level for the daemon

#### `services.vm-manager.user`
- **Type**: `string`
- **Default**: `"root"`
- **Description**: User to run the daemon as (must have libvirt access)

#### `services.vm-manager.group`
- **Type**: `string`
- **Default**: `"root"`
- **Description**: Group to run the daemon as

## Complete Examples

### Example 1: Basic Provisioning

```nix
{
  services.vm-manager = {
    enable = true;
    tags = [ "provision-me" ];
    ssh = {
      username = "root";
      keyFile = /root/.ssh/vm-manager-key;
    };
    tagToRemove = "provision-me";
    logLevel = "info";
  };
}
```

### Example 2: CI/CD Pipeline

```nix
{
  services.vm-manager = {
    enable = true;
    
    tags = [ "ci-test" ];
    excludeTags = [ "production" ];
    
    ssh = {
      username = "ci";
      keyFile = /run/secrets/ci-ssh-key;
    };
    
    bootAtStart = true;
    checkExisting = true;
    
    checkInterval = 5;
    maxWaitTime = 300;         # 5 minutes for CI (shorter than default 30 min)
    brokenTag = "ci-broken";   # Custom broken tag for CI monitoring
    onBroken = /opt/scripts/notify-broken-vm.sh;  # Alert on broken VMs
    staleScanInterval = 120;   # Scan for stale tags every 2 minutes
    
    logLevel = "debug";
  };
}
```

**Note**: The `onBroken` script receives environment variables (`VM_NAME`, `VM_UUID`, `VM_IP`, etc.) and can be used to send alerts, create tickets, or trigger auto-remediation.

### Example 3: Development Environment

```nix
{
  services.vm-manager = {
    enable = true;
    
    tags = [ "dev-vm" ];
    excludeTags = [ "production" ];
    
    ssh = {
      username = "dev";
      passwordFile = /run/secrets/dev-ssh-password;
    };
    
    bootAlways = true;
    checkExisting = true;
    
    tagToRemove = "ready";
    logLevel = "info";
  };
}
```

### Example 4: Non-root with Custom User

```nix
{
  # Create user and add to libvirt group
  users.users.vm-manager = {
    isSystemUser = true;
    group = "libvirt";
    home = "/var/lib/vm-manager";
    createHome = true;
  };

  services.vm-manager = {
    enable = true;
    
    tags = [ "managed" ];
    
    ssh = {
      username = "ansible";
      keyFile = /var/lib/vm-manager/.ssh/id_rsa;
    };
    
    user = "vm-manager";
    group = "libvirt";
    
    logLevel = "warning";
  };
}
```

## Secrets Management

### Using agenix

```nix
{
  age.secrets.vm-manager-ssh-key = {
    file = ./secrets/vm-manager-ssh-key.age;
    owner = "root";
    group = "root";
    mode = "0600";
  };

  services.vm-manager = {
    enable = true;
    tags = [ "provision-me" ];
    ssh = {
      username = "root";
      keyFile = config.age.secrets.vm-manager-ssh-key.path;
    };
  };
}
```

### Using sops-nix

```nix
{
  sops.secrets.vm-manager-ssh-key = {
    sopsFile = ./secrets.yaml;
    owner = "root";
    mode = "0600";
  };

  services.vm-manager = {
    enable = true;
    tags = [ "provision-me" ];
    ssh = {
      username = "root";
      keyFile = config.sops.secrets.vm-manager-ssh-key.path;
    };
  };
}
```

## Systemd Integration

The module creates a systemd service: `vm-manager.service`

### View logs

```bash
# Follow logs
journalctl -u vm-manager -f

# View recent logs
journalctl -u vm-manager -n 100

# Logs since boot
journalctl -u vm-manager -b
```

### Control service

```bash
# Check status
systemctl status vm-manager

# Restart
systemctl restart vm-manager

# Stop
systemctl stop vm-manager

# Start
systemctl start vm-manager
```

## Security Features

The systemd service includes hardening options:

- **NoNewPrivileges**: Prevents privilege escalation
- **PrivateTmp**: Isolated `/tmp` directory
- **ProtectSystem**: Read-only `/usr`, `/boot`, `/efi`
- **ProtectHome**: Inaccessible `/home` directories
- **ProtectKernelTunables**: Read-only kernel tunables
- **RestrictAddressFamilies**: Only Unix, IPv4, IPv6 sockets
- **MemoryMax**: Limited to 512MB
- **TasksMax**: Limited to 256 tasks

## Troubleshooting

### Service fails to start

Check logs:
```bash
journalctl -u vm-manager -xe
```

Common issues:
- SSH key file doesn't exist or has wrong permissions
- User doesn't have access to libvirt
- libvirtd service not running

### Permission denied errors

Ensure the service user has libvirt access:
```nix
{
  users.users.vm-manager.extraGroups = [ "libvirt" ];
  
  # Or run as root:
  services.vm-manager.user = "root";
}
```

### VMs not being monitored

Enable debug logging:
```nix
{
  services.vm-manager.logLevel = "debug";
}
```

Check that VMs have the correct tags:
```bash
sudo virsh desc vm-name
```

## Integration with NixOS Firewall

If your VMs need specific firewall rules:

```nix
{
  networking.firewall = {
    enable = true;
    # Allow SSH if vm-manager needs to connect through firewall
    allowedTCPPorts = [ 22 ];
  };
}
```

## Module Validation

The module performs these validations:

1. **tags is not empty**: At least one tag must be specified
2. **SSH auth provided**: Either keyFile or passwordFile must be set
3. **Boot modes exclusive**: Cannot enable both bootAtStart and bootAlways

## See Also

- [VM Manager Documentation](../docs/vm-manager/README.md)
- [Architecture](../docs/vm-manager/ARCHITECTURE.md)
- [Testing](../docs/vm-manager/TESTING.md)
- [Example Configuration](example-configuration.nix)
