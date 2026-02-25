{ config, lib, pkgs, ... }:

with lib;

let
  cfg = config.services.vm-manager;
  
  # Build configuration file content
  configFile = pkgs.writeText "vm-manager-flags" ''
    ${concatStringsSep " " (
      # Required tags
      (map (tag: "--tag ${escapeShellArg tag}") cfg.tags)
      
      # Exclude tags
      ++ (map (tag: "--exclude-tag ${escapeShellArg tag}") cfg.excludeTags)
      
      # SSH configuration
      ++ [ "--ssh-username ${escapeShellArg cfg.ssh.username}" ]
      ++ optional (cfg.ssh.keyFile != null) "--ssh-key ${escapeShellArg cfg.ssh.keyFile}"
      ++ optional (cfg.ssh.passwordFile != null) "--ssh-password-file ${escapeShellArg cfg.ssh.passwordFile}"
      
      # Tag removal
      ++ optional (cfg.tagToRemove != null) "--mark-as-used ${escapeShellArg cfg.tagToRemove}"
      
      # Boot modes
      ++ optional cfg.bootAtStart "--boot-at-start"
      ++ optional cfg.bootAlways "--boot-always"
      
      # Timing options
      ++ [ "--check-interval ${toString cfg.checkInterval}" ]
      ++ [ "--broken-timeout ${toString cfg.brokenTimeout}" ]
      ++ [ "--on-broken-delay ${toString cfg.onBrokenDelay}" ]
      
      # Broken VM tagging
      ++ (if cfg.brokenTag != null
          then [ "--broken-tag ${escapeShellArg cfg.brokenTag}" ]
          else [ "--no-broken-tag" ])
      
      # On-broken handler script
      ++ optional (cfg.onBroken != null) "--on-broken ${escapeShellArg cfg.onBroken}"
      ++ optional (cfg.onBroken != null) "--on-broken-timeout ${toString cfg.onBrokenTimeout}"
      ++ optional (cfg.onBroken != null && cfg.onBrokenRetries != null) "--on-broken-retries ${toString cfg.onBrokenRetries}"
      ++ optional (cfg.onBroken != null) "--on-broken-retry-delay ${toString cfg.onBrokenRetryDelay}"
      
      # Startup behavior
      ++ optional cfg.checkExisting "--check-existing"
      
      # Stale tag scan
      ++ [ "--stale-scan-interval ${toString cfg.staleScanInterval}" ]
      
      # Connection and logging
      ++ [ "--libvirt-uri ${escapeShellArg cfg.libvirtUri}" ]
      ++ [ "--log-level ${cfg.logLevel}" ]
    )}
  '';

in {
  options.services.vm-manager = {
    enable = mkEnableOption "VM Manager daemon for monitoring libvirt VMs and managing tags";

    package = mkOption {
      type = types.package;
      default = pkgs.vm-manager;
      defaultText = literalExpression "pkgs.vm-manager";
      description = "The vm-manager package to use.";
    };

    tags = mkOption {
      type = types.listOf types.str;
      example = [ "provision-me" "ci-test" ];
      description = ''
        List of tags to monitor. VMs must have at least one of these tags
        to be monitored.
      '';
    };

    excludeTags = mkOption {
      type = types.listOf types.str;
      default = [];
      example = [ "production" "manual-only" ];
      description = ''
        List of tags to exclude. VMs with any of these tags will not be
        monitored, even if they have a monitored tag.
      '';
    };

    ssh = {
      username = mkOption {
        type = types.str;
        example = "root";
        description = "SSH username for connectivity checks.";
      };

      keyFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        example = "/run/secrets/vm-manager-ssh-key";
        description = ''
          Path to SSH private key file. Either keyFile or passwordFile
          must be specified.
        '';
      };

      passwordFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        example = "/run/secrets/vm-manager-ssh-password";
        description = ''
          Path to file containing SSH password. Either keyFile or
          passwordFile must be specified.
        '';
      };
    };

    tagToRemove = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "used";
      description = ''
        Tag to remove when SSH becomes available. If null, removes the
        monitored tags instead.
      '';
    };

    bootAtStart = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Boot all matching shutdown VMs once at daemon startup.
        Mutually exclusive with bootAlways.
      '';
    };

    bootAlways = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Continuously boot matching shutdown VMs (daemon monitors and boots them).
        Mutually exclusive with bootAtStart.
      '';
    };

    checkInterval = mkOption {
      type = types.ints.positive;
      default = 10;
      description = "Interval between SSH retry attempts in seconds.";
    };

    brokenTimeout = mkOption {
      type = types.ints.unsigned;
      default = 300;
      example = 600;
      description = ''
        Time in seconds to wait for SSH before marking the VM as broken.
        Phase 1 of the two-phase timeout: SSH is retried every check-interval
        seconds. If SSH fails after this timeout, the broken tag is added
        and Phase 2 begins. Set to 0 for immediate broken tagging.
        Default: 300 (5 minutes).
      '';
    };

    onBrokenDelay = mkOption {
      type = types.ints.unsigned;
      default = 1500;
      example = 900;
      description = ''
        Time in seconds to wait after marking a VM broken before running the
        on-broken script. Phase 2 of the two-phase timeout: SSH monitoring
        continues during this delay. If SSH succeeds, the broken tag is removed
        and the VM returns to normal. Only relevant when onBroken is set;
        without a script, broken VMs are monitored indefinitely.
        Set to 0 to run the script immediately after the broken tag.
        Default: 1500 (25 minutes).
      '';
    };

    brokenTag = mkOption {
      type = types.nullOr types.str;
      default = "broken";
      example = "needs-repair";
      description = ''
        Tag to add to VMs that fail SSH after brokenTimeout. The 'used' tag is
        kept so the VM won't be reallocated. Set to null to disable broken tagging.
      '';
    };

    onBroken = mkOption {
      type = types.nullOr types.path;
      default = null;
      example = literalExpression ''/path/to/handler.sh'';
      description = ''
        Path to an external script/program to run when a VM is marked broken.
        The script receives VM information via environment variables:
        VM_NAME, VM_UUID, VM_IP, VM_TAGS, VM_BROKEN_TAG, VM_WAIT_TIME, LIBVIRT_URI.
        The script is retried on failure according to onBrokenRetries and
        onBrokenRetryDelay. Set to null to disable (default).
      '';
    };

    onBrokenTimeout = mkOption {
      type = types.ints.positive;
      default = 300;
      example = 600;
      description = ''
        Maximum time in seconds to wait for the on-broken script to finish
        before killing it. Default: 300 (5 minutes).
      '';
    };

    onBrokenRetries = mkOption {
      type = types.nullOr types.ints.unsigned;
      default = null;
      example = 5;
      description = ''
        Maximum number of times to retry the on-broken script if it fails
        (non-zero exit or timeout). Set to null for unlimited retries (default).
        Set to 0 for no retries (run once only).
      '';
    };

    onBrokenRetryDelay = mkOption {
      type = types.ints.positive;
      default = 60;
      example = 120;
      description = ''
        Delay in seconds between on-broken script retry attempts. Default: 60.
      '';
    };

    checkExisting = mkOption {
      type = types.bool;
      default = false;
      description = ''
        Check existing running VMs at startup and remove tags if SSH is ready.
      '';
    };

    staleScanInterval = mkOption {
      type = types.ints.unsigned;
      default = 300;
      example = 600;
      description = ''
        Interval in seconds between periodic scans for stale 'used' tags.
        Removes tags from VMs that are no longer actively in use but still
        have a 'used' tag (e.g., because the VM was never rebooted after
        the deploy finished). Set to 0 to disable. Default: 300 (5 minutes).
      '';
    };

    libvirtUri = mkOption {
      type = types.str;
      default = "qemu:///system";
      description = "Libvirt connection URI.";
    };

    logLevel = mkOption {
      type = types.enum [ "debug" "info" "warning" "error" ];
      default = "info";
      description = "Log level for the daemon.";
    };

    user = mkOption {
      type = types.str;
      default = "root";
      description = ''
        User to run the VM Manager daemon as. Must have access to libvirt.
      '';
    };

    group = mkOption {
      type = types.str;
      default = "root";
      description = "Group to run the VM Manager daemon as.";
    };
  };

  config = mkIf cfg.enable {
    # Validation
    assertions = [
      {
        assertion = cfg.tags != [];
        message = "services.vm-manager.tags must not be empty";
      }
      {
        assertion = cfg.ssh.keyFile != null || cfg.ssh.passwordFile != null;
        message = "Either services.vm-manager.ssh.keyFile or services.vm-manager.ssh.passwordFile must be set";
      }
      {
        assertion = !(cfg.bootAtStart && cfg.bootAlways);
        message = "services.vm-manager.bootAtStart and services.vm-manager.bootAlways are mutually exclusive";
      }
    ];

    # Systemd service
    systemd.services.vm-manager = {
      description = "VM Manager - Monitor VMs and manage tags based on SSH connectivity";
      documentation = [ "https://github.com/dvaerum/ansible-vm-deployer" ];
      
      after = [ "network.target" "libvirtd.service" ];
      wants = [ "libvirtd.service" ];
      wantedBy = [ "multi-user.target" ];

      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        
        # Security hardening
        NoNewPrivileges = true;
        PrivateTmp = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
        RestrictNamespaces = true;
        LockPersonality = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        
        # Resource limits
        MemoryMax = "512M";
        TasksMax = 256;
        
        # Restart policy
        Restart = "on-failure";
        RestartSec = "10s";
        
        # Logging
        StandardOutput = "journal";
        StandardError = "journal";
        SyslogIdentifier = "vm-manager";
        
        # Execute command
        ExecStart = "${cfg.package}/bin/vm-manager ${readFile configFile}";
      };
    };

    # Ensure libvirt is enabled
    virtualisation.libvirtd.enable = mkDefault true;
  };
}
