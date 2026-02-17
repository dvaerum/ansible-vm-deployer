# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed - Shared Library

- **Fix: Loopback IPs returned by `get_vm_ip()`** — The shared `get_vm_ip()` in `vm_tools_common` now filters out `127.*` loopback addresses, returning `None` instead. Previously, loopback filtering was only in the vm-manager, so ansible-deployer would pass `127.0.0.1` to Ansible (causing SSH-to-localhost failures). Both tools now benefit from the shared filter.

### Fixed - VM Manager (Critical Review)

- **Fix: Retry loop holds tracker slot** — When `_run_on_broken_script` ran inside `_monitor_vm`, it held the vm_tracker session. If the script restarted the VM, the new start event was debounced (tracker occupied) AND auto-excluded (broken tag). After script success, the VM was stranded with stale tags. Now `_monitor_vm` frees the tracker slot before running the on-broken script, and on successful repair, removes the broken tag and triggers fresh SSH monitoring via `handle_vm_started()`.
- **Fix: CancelledError orphans child process** — `_execute_on_broken_script` now properly kills the subprocess when a `CancelledError` occurs during `communicate()`, preventing orphaned processes.
- **Fix: `--on-broken-retry-delay 0` allows busy-wait spin** — CLI now validates that `--on-broken-retry-delay` must be at least 1 second (NixOS module already uses `ints.positive`).
- **Fix: `--on-broken-timeout 0` causes infinite kill loop** — CLI now validates that `--on-broken-timeout` must be at least 1 second.
- **Fix: `--on-broken-retries -1` accepted without validation** — CLI now validates that `--on-broken-retries` must be a non-negative integer.

### Changed - Ansible Deployer

- **`--repeat N` for repeated playbook execution** — New `deploy` argument that runs the playbook N times on the same VM without reset between iterations. Stops on first failure. Each iteration gets its own log files (`_run-1`, `_run-2`, etc.). Default is 1 (backward compatible — no suffix added).
- **`--repeat` metavar cleanup** — Help text now shows `--repeat N` instead of the confusing `--repeat INTEGER RANGE`.

- **`--log-prefix` now supports subdirectories** — Using `--log-prefix test/linux` creates log files under `logs/test/linux_<timestamp>_stdout.log` instead of replacing `/` with `-`. Parent directories are created automatically. Other special characters (spaces, dots, etc.) are still replaced with hyphens.

### Added - VM Manager

- **Periodic stale tag scan** — New `--stale-scan-interval` option (default: 300 seconds / 5 minutes) periodically scans all running VMs and removes stale `used` tags directly, without waiting for SSH. A tag is stale when the VM has a removable tag (e.g., `used`) in its inactive XML but is no longer actively in use by ansible-deployer (`in_use=false` or no metadata). This catches VMs where the deployer finished but the VM was never rebooted, so vm-manager never got a lifecycle event to trigger cleanup.
  - New CLI option: `--stale-scan-interval SECONDS` (default: 300, 0 = disabled)
  - New NixOS option: `services.vm-manager.staleScanInterval` (default: 300)
  - Startup scan (`--check-existing`) also uses this logic: stale VMs get tags removed directly, actively-in-use VMs go through SSH monitoring

### Fixed - VM Manager

#### Race Condition Fixes (Production Stress-Tested)

All fixes were validated with 10 consecutive stress test runs (80 parallel ansible-deployer jobs total, 30 concurrent VMs) with zero stale tags remaining.

- **Fix: Reboot events not detected** — Added `VIR_DOMAIN_EVENT_ID_REBOOT` listener alongside the existing lifecycle event listener. Previously, VMs rebooted by `reset_vm()` (via QEMU guest agent) did not generate lifecycle start/stop events, only reboot events, so vm-manager never detected them.

- **Fix: SSH race condition with stale boots** — Added uptime verification (< 120 seconds) after SSH succeeds. Previously, vm-manager could SSH into a VM that had been running for hours (from a previous boot) and incorrectly conclude it was a fresh boot ready for tag removal.

- **Fix: Cleanup race with ansible-deployer** — Added a 5-second delay before tag removal to ensure ansible-deployer's `reset_vm()` + `mark_available()` cleanup has fully completed. The non-blocking reboot means metadata is cleared before the VM finishes rebooting.

- **Fix: Stale `used` tags on startup with `--check-existing`** — Added `_is_vm_stale()` check when processing existing running VMs at startup. VMs with a `used` tag but no active deployer session (no `in_use` metadata) are now correctly identified as stale and have their tags removed directly, instead of starting infinite SSH retry loops.

- **Fix: Orphaned monitor tasks for VMs without removable tags** — Added tag check in `_handle_vm_started()` to verify the VM actually has tags that need removing before starting a monitor. Previously, when `reset_vm()` rebooted a VM after the `used` tag was already removed, vm-manager would start a new infinite SSH retry loop. These orphaned monitors accumulated without bound.

- **Fix: Loopback IP (127.0.0.1) during boot** — `_get_vm_ip_with_retry()` retries until a routable IP is available. Loopback addresses (`127.*`) are now filtered by the shared `get_vm_ip()` in `vm_tools_common` (see "Fixed - Shared Library" above).

- **Fix: Mid-run tag removal race condition** — Added `in_use` metadata check before removing the `used` tag. A playbook may reboot the VM mid-run (e.g., OS install), triggering vm-manager to detect the reboot and eventually SSH in. Without this check, vm-manager would remove the `used` tag while ansible-deployer was still actively orchestrating the VM, allowing another deployer to allocate it concurrently.

- **Fix: VMs with no IP never marked broken** — IP resolution is now part of the overall `--max-wait-time` budget. Previously, if a VM never obtained an IP address, `_monitor_vm()` gave up after 30 seconds (10 attempts) and returned without marking the VM as broken. The VM would then reboot, vm-manager would start monitoring again, fail to get an IP, and the cycle would repeat forever. Now, IP resolution retries within the `max_wait_time` window, and if no IP is found within the budget, the VM is marked broken (with `--broken-tag`) and the `--on-broken` script is called, just like an SSH timeout. The `wait_for_ssh()` method accepts a `max_wait_time_override` parameter so the remaining time budget is correctly passed without mutating shared state across concurrent VMs.

### Added - VM Manager (cont.)

- **Broken VM tagging** — VMs that fail SSH after `--max-wait-time` (default: 30 minutes) are now tagged with a configurable `--broken-tag` (default: `broken`) instead of retrying forever. The `used` tag is intentionally kept so the VM won't be reallocated by ansible-deployer.
  - New CLI options: `--broken-tag TAG` (default: `broken`), `--no-broken-tag`
  - New NixOS option: `services.vm-manager.brokenTag` (default: `"broken"`)

- **Auto-exclude broken VMs** — Both vm-manager and ansible-deployer now automatically exclude VMs tagged as `broken`:
  - **vm-manager daemon**: Automatically appends `broken_tag` to `exclude_tags` on startup, preventing infinite re-monitoring loops where a broken VM would be detected, wait 30 minutes for SSH timeout, get re-tagged as broken, and repeat.
  - **ansible-deployer**: `allocate_vms()`, `find_available_vm_by_tags()`, and `find_available_vms_by_tags()` automatically exclude VMs with the `broken` tag, preventing allocation of VMs that can't be reached via SSH.

- **`--on-broken` script hook** — Optional external script called when a VM is marked broken. Enables integration with alerting, ticketing, or auto-remediation systems.
  - New CLI option: `--on-broken /path/to/handler.sh` (must be an executable file)
  - New NixOS option: `services.vm-manager.onBroken` (type: `nullOr path`, default: `null`)
  - Environment variables passed to script: `VM_NAME`, `VM_UUID`, `VM_IP`, `VM_TAGS`, `VM_BROKEN_TAG`, `VM_WAIT_TIME`, `LIBVIRT_URI`
  - Script timeout is now configurable via `--on-broken-timeout` (default: 300 seconds, was hardcoded 60 seconds)
  - Script now retries on failure (non-zero exit or timeout) with configurable retry count and delay
  - New CLI options:
    - `--on-broken-timeout SECONDS` (default: 300) — max time before killing script
    - `--on-broken-retries COUNT` (default: unlimited) — max retry attempts (omit for infinite)
    - `--on-broken-retry-delay SECONDS` (default: 60) — delay between retries
  - New NixOS options: `onBrokenTimeout` (default: 300), `onBrokenRetries` (default: null = unlimited), `onBrokenRetryDelay` (default: 60)

- **`scripts/reset-vm-disks.sh`** — New on-broken script for resetting VM disks. Handles both file-backed disks (`<source file=.../>`) and pool-backed volumes (`<source pool=... volume=.../>`). Parses VM inactive XML directly, force-stops the VM (broken VMs are unresponsive, so graceful ACPI shutdown is unreliable), recreates disks at their original size, and restarts the VM.

- **Default SSH timeout** — `--max-wait-time` now defaults to 1800 seconds (30 minutes) instead of infinite. This prevents unbounded resource accumulation from monitors that can never succeed.
  - NixOS option `services.vm-manager.maxWaitTime` default changed from `null` to `1800`

### Changed - VM Manager

- **`ssh_checker.wait_for_ssh()` return type** — Changed from `bool` to `str` (`"success"`, `"auth_failure"`, `"timeout"`) to allow callers to distinguish between timeout and other failure modes.

## [0.2.0] - 2026-02-11

### Added - VM Manager (New Tool)

**A new tool for automated VM monitoring and tag-based provisioning workflows.**

#### Core Features
- **Event-driven VM monitoring**: Reacts to libvirt lifecycle events (domain start/stop) instead of polling
- **SSH connectivity verification**: Verifies full SSH authentication, not just TCP connection
- **Intelligent retry logic**: Handles transient failures with exponential backoff
  - SSH retry: Up to configurable attempts with increasing delays
  - IP address retry: Up to 10 attempts over 30 seconds (handles DHCP lease renewal)
- **Debouncing**: Prevents duplicate processing during VM reboots
- **Automatic tag management**: Removes tags when VMs are ready (e.g., remove "provision-me", add "used")
- **Boot management modes**:
  - `--boot-at-start`: Boot all matching shutdown VMs once at daemon start
  - `--boot-always`: Continuous loop - keep booting matching shutdown VMs
  - `--check-existing`: Process already-running VMs at daemon startup
- **Graceful shutdown**: Clean signal handling (SIGTERM/SIGINT) with async task cancellation
- **Parallel processing**: Handle multiple VMs concurrently using asyncio
- **Flexible authentication**: Support for SSH keys, passwords (from file), or both

#### Command-Line Interface
- `--tag TAG` / `--exclude-tag TAG`: Filter VMs by tags
- `--ssh-username USER`: SSH username for connectivity checks
- `--ssh-key PATH`: Path to SSH private key
- `--ssh-password-file PATH`: Path to file containing SSH password
- `--mark-as-used [TAG]`: Remove tags when VM is ready (default: "used")
- `--boot-at-start` / `--boot-always`: Boot mode selection
- `--check-existing`: Process running VMs at startup
- `--check-interval SECONDS`: SSH retry interval (default: 5s)
- `--max-wait-time SECONDS`: Maximum time to wait for SSH (default: 1800s)
- `--libvirt-uri URI`: Libvirt connection URI
- `--log-level LEVEL`: Logging verbosity (debug/info/warning/error)

#### Architecture
- **Event-driven design**: No polling, reacts to VM state changes
- **Async-first**: All I/O operations use asyncio for efficient concurrency
- **Modular components**:
  - `EventMonitor`: Libvirt event loop integration
  - `SSHChecker`: SSH connectivity verification with retry logic
  - `TagCleaner`: Tag cleanup orchestration
  - `VMTracker`: Session management and debouncing
  - `Daemon`: Main orchestration with signal handling
- **Shared library**: `vm_tools_common` provides common VM operations for both ansible-deployer and vm-manager
- **Defensive programming**: Comprehensive error handling and graceful degradation

#### NixOS Integration
- **Complete NixOS module** (`services.vm-manager`):
  - Declarative configuration for all options
  - Systemd service with security hardening:
    - DynamicUser with proper permissions
    - NoNewPrivileges, PrivateTmp, ProtectSystem
    - Resource limits (512MB memory, 256 tasks)
  - Proper service dependencies (libvirtd.service)
  - Validation for required options
  - Integration examples for agenix/sops-nix secrets
- **Nix packages**:
  - Separate packages: `ansible-deployer`, `vm-manager`
  - Overlay (`overlays.default`) for package distribution
  - Apps configured for both tools
- **Example configurations**: 4 real-world scenarios (basic, CI/CD, dev environments, non-root)

#### Documentation
- **User Guide** (`docs/vm-manager/README.md`, 510 lines):
  - Complete overview and feature list
  - Installation and quick start
  - Comprehensive CLI reference
  - Real-world use cases (provisioning, CI/CD, dev environments)
  - Troubleshooting guide
  - Security best practices
  - Performance tuning
- **Architecture Guide** (`docs/vm-manager/ARCHITECTURE.md`, 759 lines):
  - Design principles and philosophy
  - Component architecture with diagrams
  - Data flow diagrams (normal flow, debouncing, error handling)
  - Key design decisions with rationale
  - Concurrency model and task management
  - Error handling patterns
  - Shared library architecture
- **Testing Guide** (`docs/vm-manager/TESTING.md`, 740 lines):
  - Test suite overview
  - Running tests (all tests, specific files, coverage)
  - Detailed test descriptions for each component
  - Manual testing procedures (8 step-by-step scenarios)
  - Test development guidelines
  - CI/CD integration examples
- **NixOS Module Documentation** (`nixos-modules/README.md`, 395 lines):
  - Quick start guide
  - All configuration options documented
  - Complete examples with explanations
  - Secrets management integration
  - Systemd service integration
  - Troubleshooting guide

#### Test Suite
- **346 comprehensive unit tests** covering all 7 race condition fixes, broken tag feature, auto-exclude behavior, `--on-broken` script hook (with retry/timeout), repair flow, CancelledError handling, and stale tag scanning:
  - `tests/conftest.py`: Shared fixtures (`make_mock_domain()`, `make_mock_conn()`)
  - `tests/test_tag_filters.py`: 14 tests (`vm_matches_tags()` — required/exclude tags)
  - `tests/test_vm_operations.py`: 45 tests (tag CRUD, IP resolution, state strings)
  - `tests/test_metadata_manager.py`: 41 tests (MetadataManager get/set/claim/clear)
  - `tests/test_allocate_vms.py`: 37 tests (VM allocation with auto-exclude broken)
  - `tests/vm_manager/test_daemon.py`: 70 tests (event filtering, stale tags, startup scan, stale scan loop, auto-exclude broken_tag, on-broken timeout/retries/delay init)
  - `tests/vm_manager/test_tag_cleaner.py`: 59 tests (tag removal orchestration, race conditions #3/#6/#7, broken tagging, in_use check, on-broken script hook, retry logic, configurable timeout, return values, repair flow, CancelledError handling)
  - `tests/vm_manager/test_ssh_checker.py`: 23 tests (uptime verification, string return values)
  - `tests/vm_manager/test_event_monitor.py`: 15 tests (reboot callback registration)
  - `tests/vm_manager/test_vm_tracker.py`: 7 tests (session management, debouncing)
- **8 manual tests**: All scenarios validated with real VMs
- **100% test pass rate**: 346/346 total tests
- **Mocked dependencies**: No real VMs or libvirt connection needed for unit tests

#### Bug Fixes (During Development)
- Fixed libvirt connection usage (use `LibvirtConnection` class)
- Fixed parameter names (`exclude_tags` instead of `excluded_tags`)
- Added IP address retry logic to handle DHCP lease renewal during rapid VM restarts
  - Retries up to 10 times over 30 seconds
  - Prevents failures when VMs restart quickly (e.g., provisioning loops)

#### Use Cases
1. **Automated VM Provisioning**: Tag VMs as "provision-me", vm-manager detects when they're SSH-ready, removes tag
2. **CI/CD VM Pools**: Monitor VM pools, automatically mark VMs as ready when provisioning completes
3. **Development Environments**: Auto-boot and monitor dev VMs, track when they're ready for use
4. **Self-Service VM Allocation**: Users request VMs via tags, vm-manager verifies readiness

### Added - Shared Library

**Extracted common VM operations into reusable library.**

- Created `vm_tools_common` package with shared functionality:
  - `vm_operations.py`: Common VM tag operations (add, remove, get tags)
  - `libvirt_connection.py`: Connection management
  - `tag_filters.py`: Tag filtering logic
  - `exceptions.py`: Common exceptions
- Both `ansible-deployer` and `vm-manager` now use shared library
- Eliminates code duplication
- Ensures consistent behavior across tools

### Changed

- **Project renamed conceptually**: Now "VM Management Tools for Libvirt" (suite of tools)
- **Main README updated**: 
  - Documents both tools (ansible-deployer + vm-manager)
  - Updated project structure showing 3 packages
  - Added VM Manager quick start examples
  - Updated documentation links
  - Updated test suite information (56 total tests)
- **Nix flake refactored**:
  - Separated packages for ansible-deployer and vm-manager
  - Added overlay for package distribution
  - Exported NixOS module for vm-manager
  - Configured apps for both tools

### Performance

- **Scalability**: Tested with 30+ concurrent VMs
- **Resource usage**: 
  - Memory: ~50MB baseline (systemd limit: 512MB)
  - CPU: Minimal (event-driven, no polling)
- **Response time**: 
  - Event detection: <1 second (libvirt callback)
  - SSH verification: 2-15 seconds (with retry)
  - Tag cleanup: <1 second

### Added
- `--project-root` global flag to set base directory for all relative paths
- `--log-dir` global flag to specify custom log directory (works across all commands)
- `--log-prefix` option to add custom prefix to log filenames for better organization (e.g., `--log-prefix prod-deploy`)
- `--quiet` flag to suppress Ansible output to console while still writing to log files (useful for CI/CD and background deployments)
- Project root support: playbook, inventory, and log paths are relative to project root
- Wrapper script auto-detection at `<project-root>/ansible-wrapper.sh`
- Fallback to `ansible-playbook` directly if wrapper script not found
- Optional `--inventory` flag for deploy command to specify custom Ansible inventory files
- Example inventory file (`playbooks/inventory.example.ini`)
- Documentation for inventory file usage in README.md and USAGE.md
- Documentation for project root feature in README.md and USAGE.md
- Documentation for wrapper script arguments and environment variables
- `--ansible-flags` option to pass arbitrary flags to ansible-playbook (e.g., `-vvv`, `--check`, `--diff`)
- **Real-time log writing**: stdout log file is now written line-by-line as Ansible executes (use `tail -f` to monitor progress)
- CLI shows log file path at start of deployment for easy monitoring
- Documentation for real-time progress monitoring
- **Wrapper script working directory**: When `--project-root` is set, wrapper script runs with cwd set to project root (allows relative paths)
- **Custom Ansible callback plugin**: Provides both real-time output AND structured JSON logging
  - Real-time stdout log for monitoring with `tail -f`
  - Complete JSON output with all task results and stats
  - No callback warnings or errors
- **VM usage tagging**: `--mark-in-use[=TAG]` and `--mark-available` options
  - Add usage tags to VM descriptions to track purpose
  - Default tag "used", or specify custom tag
  - Optional automatic removal after reset with `--mark-available`
  - Integrates with `--tag` and `--exclude-tag` for sophisticated VM selection
- **Passthrough arguments**: Pass additional arguments to wrapper script or ansible-playbook
  - Use `--` separator to pass arguments directly: `deploy --tag test --playbook foo.yml -- --check --diff`
  - Arguments appended to end of command
  - Useful for one-off flags like `--syntax-check`, `--step`, `--limit`, etc.
- **`--json` flag for `list-vms` and `status`**: Machine-readable JSON output for scripting and CI/CD integration
  - `list-vms --json`: Returns array of VM objects with proper types (tags as arrays, in_use as booleans)
  - `status --vm-name <name> --json`: Returns full VM details including metadata, networks, interfaces, and default IP
- **Tags column in `list-vms`**: Table output now shows VM tags between State and In Use columns
- Comprehensive documentation for new features in USAGE.md

### Changed
- **Removed JSON callback**: Ansible now uses default callback for real-time output instead of JSON callback which buffers everything until the end
- Log files: stdout.log is written in real-time (monitor this), json.log only contains metadata

### Changed
- **Simplified config file**: Only `libvirt_uri` remains in `config.yaml`
  - Removed `log_dir` (use `--log-dir` CLI option, default: `./logs`)
  - Removed `log_level` (use `--log-level` CLI option, default: `INFO`)
  - Removed `network` (use `--network` on deploy command)
  - Removed `ansible_verbose` (use `--ansible-flags` or `--verbose`)
  - Removed `ansible_wrapper_script` (auto-detection still works)
  - Removed `reset_timeout` (non-blocking reset, no longer needed)
- `--log-level` added as global CLI option with choices: `debug`, `info`, `warning`, `error`
- Verbosity and other ansible options now passed via `--ansible-flags` for maximum flexibility
- **VM reset is now non-blocking**: Wipefs and reboot are initiated but tool doesn't wait for VM to come back
  - Eliminates 5-minute timeout errors when waiting for guest agent
  - Reset completes in ~2-3 seconds instead of 15-35 seconds
  - VMs reboot in background and are ready for next deployment when finished
  - Removed `reset_timeout` configuration option (no longer needed)

### Removed
- **`list-logs` subcommand**: Removed (use `ls` on the log directory instead)
- **`show-log` subcommand**: Removed (use `cat`/`less` on log files directly)

### Fixed
- **Test suite**: Fixed all 15 tests to pass correctly
  - Updated config tests to only assert `libvirt_uri` (the only remaining Config field after simplification)
  - Fixed MetadataManager mock tests to raise `libvirt.libvirtError` instead of generic `Exception` (matches `set_metadata_bulk()` catch clause)
  - Fixed VMManager `get_vm_by_name` mock test to raise `libvirt.libvirtError` instead of generic `Exception` (matches `get_vm_by_name()` catch clause)
  - Cleaned up stale assertions on removed config fields (`log_level`, `reset_timeout`, `ansible_verbose`, `log_dir`)
- `--log-dir` moved to global options so all commands can access custom log directories
- **Libvirt stderr noise suppressed**: Registered custom error handler to silence `metadata not found` messages printed by libvirt's C library when VMs have no metadata initialized
- Wrapper script gracefully handles missing file (falls back to ansible-playbook)
- **VM reset implementation**: Fixed to use `libvirt_qemu` module for guest agent commands
  - Now uses correct API: `libvirt_qemu.qemuAgentCommand()` instead of non-existent `domain.qemuAgentCommand()`
  - Detects when `guest-exec` command is disabled in VM (default RHEL/CentOS security setting)
  - Skips reset entirely when guest-exec disabled (no pointless reboot without disk wipe)
  - Logs clear instructions on how to enable guest-exec if desired
- **Ansible callback plugin**: Fixed to produce real Ansible JSON output instead of fake metadata
  - Added proper documentation fragment extensions (`default_callback`, `result_format_callback`)
  - Removed non-existent callback methods that caused warnings
  - Implemented proper data collection using existing callback hooks
  - JSON output now contains actual task results with complete Ansible output
- **VM tag functions**: Fixed to use inactive/persistent XML configuration
  - `get_vm_tags()` now reads persistent config (VIR_DOMAIN_XML_INACTIVE)
  - Tag modifications work for both running and stopped VMs
  - Tags persist across VM lifecycle operations
- **Race-condition-safe VM allocation**: Atomic claim-and-verify prevents double-allocation
  - New `allocate_vms()` method claims VMs during search using `try_claim()`
  - `set_metadata_bulk()` writes all metadata fields (in_use, task_id, started_at) in a single `setMetadata()` call, preventing interleaving from concurrent processes
  - `try_claim()` adds 150ms delay before verification to let concurrent writers finish, ensuring last-writer-wins is correctly detected
  - Partial claims are released and retried if not enough VMs claimed
  - Verified with 15 simultaneous processes across 6 consecutive runs (90 allocations total, zero conflicts)
- **Metadata XML namespace handling**: Fixed inconsistent namespace parsing in libvirt metadata
  - New `_find_element()` helper handles both `avm:` and `ns0:` prefixed elements
  - Prevents duplicate metadata elements from accumulating across operations
- **Error handling**: Graceful handling of libvirt authentication failures
  - No more stack traces for polkit/authentication errors
  - User-friendly error messages with actionable solutions
  - Detects authentication, connection, and permission issues
  - Provides specific troubleshooting steps (sudo, libvirt group, polkit)
  - Separate handling for expected vs unexpected errors
  - `get_vm_tags()` now reads persistent config (VIR_DOMAIN_XML_INACTIVE)
  - Tag modifications work for both running and stopped VMs
  - Tags persist across VM lifecycle operations
  - Tool handles all scenarios gracefully without crashing

## [0.1.0] - 2026-02-09

### Added
- Initial release of Ansible Deployer
- Tag-based VM selection with include/exclude filters
- Multi-VM allocation with configurable wait/retry logic
- Network-based interface selection for multi-network environments
- Environment variable injection (VM_IP_1, VM_IP_2, VM_IP_ALL)
- Customizable ansible-wrapper.sh for flexible Ansible execution
- Dual logging system (JSON + human-readable formats)
- Automatic VM reset functionality with wipefs
- Metadata management using libvirt XML
- Rich CLI with beautiful terminal output
- Comprehensive documentation (Quick Start, Usage Guide, Tagging Guide)
- Example playbooks and code samples
- Nix flake for reproducible development environment
- Test suite with integration tests
- MIT License

### Features
- Deploy Ansible playbooks to one or more VMs automatically
- Automatic VM allocation and cleanup
- Concurrent deployment support
- Configurable timeouts and retry logic
- Professional Python package structure with type hints
