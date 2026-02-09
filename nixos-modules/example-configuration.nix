# Example NixOS configuration using the vm-manager module
#
# To use this module in your NixOS configuration:
#
# 1. Add the flake to your system flake inputs:
#    inputs.vm-tools.url = "github:dvaerum/ansible-vm-deployer";
#
# 2. Import the module:
#    imports = [ vm-tools.nixosModules.vm-manager ];
#
# 3. Apply the overlay (optional, for packages):
#    nixpkgs.overlays = [ vm-tools.overlays.default ];

{ config, pkgs, ... }:

{
  # Example 1: Basic VM monitoring with tag removal
  services.vm-manager = {
    enable = true;
    
    tags = [ "provision-me" ];
    
    ssh = {
      username = "root";
      keyFile = /root/.ssh/vm-manager-key;
    };
    
    tagToRemove = "provision-me";
  };

  # Example 2: CI/CD pipeline with boot-at-start
  # services.vm-manager = {
  #   enable = true;
  #   
  #   tags = [ "ci-test" ];
  #   excludeTags = [ "production" ];
  #   
  #   ssh = {
  #     username = "ci";
  #     keyFile = /run/secrets/ci-ssh-key;  # Using agenix/sops-nix
  #   };
  #   
  #   bootAtStart = true;
  #   checkExisting = true;
  #   
  #   checkInterval = 5;
  #   maxWaitTime = 300;  # 5 minutes max
  #   
  #   logLevel = "debug";
  # };

  # Example 3: Development environment with continuous boot
  # services.vm-manager = {
  #   enable = true;
  #   
  #   tags = [ "dev-vm" ];
  #   excludeTags = [ "production" "manual" ];
  #   
  #   ssh = {
  #     username = "dev";
  #     passwordFile = /run/secrets/dev-ssh-password;
  #   };
  #   
  #   bootAlways = true;
  #   checkExisting = true;
  #   
  #   tagToRemove = "ready";
  #   
  #   libvirtUri = "qemu:///system";
  #   logLevel = "info";
  # };

  # Example 4: Multiple tags with custom timing
  # services.vm-manager = {
  #   enable = true;
  #   
  #   tags = [ "test-vm" "staging-vm" ];
  #   excludeTags = [ "production" ];
  #   
  #   ssh = {
  #     username = "ansible";
  #     keyFile = /var/lib/vm-manager/ansible-key;
  #   };
  #   
  #   checkInterval = 15;
  #   maxWaitTime = 600;  # 10 minutes
  #   
  #   checkExisting = true;
  #   
  #   # Run as non-root user (must be in libvirt group)
  #   user = "vm-manager";
  #   group = "libvirt";
  #   
  #   logLevel = "warning";
  # };
}
