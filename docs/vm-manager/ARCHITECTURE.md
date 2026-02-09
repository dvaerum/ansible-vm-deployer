# VM Manager Architecture

This document describes the internal architecture, design decisions, and component interactions of the VM Manager daemon.

## Table of Contents

- [Overview](#overview)
- [Design Principles](#design-principles)
- [Component Architecture](#component-architecture)
- [Data Flow](#data-flow)
- [Key Design Decisions](#key-design-decisions)
- [Error Handling](#error-handling)
- [Concurrency Model](#concurrency-model)

---

## Overview

VM Manager is an event-driven daemon built with Python asyncio, designed to monitor libvirt VMs and manage their lifecycle based on SSH connectivity and tag metadata.

### Core Technologies

- **Python 3.11+**: Modern Python with async/await support
- **asyncio**: Event loop for concurrent operations
- **libvirt**: VM lifecycle event monitoring
- **paramiko**: SSH connectivity verification
- **pytest**: Comprehensive test framework

---

## Design Principles

### 1. **Event-Driven Architecture**

Rather than polling, VM Manager reacts to libvirt events:
- Lower CPU usage (no constant polling)
- Immediate reaction to VM lifecycle changes
- Scalable to many VMs

### 2. **Separation of Concerns**

Each component has a single, well-defined responsibility:
- **EventMonitor**: Only handles libvirt events
- **SSHChecker**: Only verifies SSH connectivity
- **VMTracker**: Only manages session state
- **TagCleaner**: Only orchestrates the workflow
- **Daemon**: Only coordinates components

### 3. **Async-First Design**

All I/O operations are asynchronous:
- Multiple VMs can be monitored concurrently
- Non-blocking SSH checks
- Efficient resource utilization

### 4. **Defensive Programming**

- Retry logic for transient failures (IP resolution, SSH connections)
- Graceful degradation (one VM failure doesn't affect others)
- Comprehensive error logging
- User-friendly error messages (no stack traces for expected errors)

### 5. **Testability**

- Mocked dependencies (libvirt, paramiko) for unit tests
- Clear interfaces between components
- 100% test coverage of core logic

---

## Component Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      VMManagerDaemon                        │
│                                                             │
│  Responsibilities:                                          │
│  • Initialize all components                                │
│  • Handle signals (SIGTERM, SIGINT)                         │
│  • Coordinate startup/shutdown                              │
│  • Run boot modes (--boot-at-start, --boot-always)          │
└──────────┬──────────────┬──────────────┬───────────────────┘
           │              │              │
           ▼              ▼              ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│EventMonitor  │  │  VMTracker   │  │ TagCleaner   │
│              │  │              │  │              │
│• Libvirt     │  │• Session     │  │• IP retry    │
│  event loop  │  │  registry    │  │• SSH coord.  │
│• Callbacks   │  │• Debouncing  │  │• Tag removal │
│• Start/stop  │  │• Async locks │  │              │
└──────────────┘  └──────────────┘  └──────┬───────┘
                                            │
                                            ▼
                                   ┌──────────────┐
                                   │ SSHChecker   │
                                   │              │
                                   │• Paramiko    │
                                   │• Retry logic │
                                   │• Timeout     │
                                   └──────────────┘
```

### 1. VMManagerDaemon

**Purpose**: Main orchestrator and entry point

**Responsibilities**:
- Initialize libvirt connection
- Create and wire together all components
- Handle startup modes (--check-existing, --boot-at-start)
- Run continuous boot loop (--boot-always)
- Handle graceful shutdown
- Signal handling

**Key Methods**:
- `start()`: Initialize and start all components
- `run()`: Main event loop (waits for shutdown)
- `stop()`: Cleanup and shutdown
- `_handle_vm_started()`: VM start event callback
- `_handle_vm_stopped()`: VM stop event callback (boot-always)

**Auto-Exclude Broken VMs**: On initialization, the daemon automatically appends `broken_tag` to `exclude_tags` (copying the list to avoid mutating the caller's). This prevents infinite re-monitoring loops where a broken VM would be detected on reboot/`--check-existing`, wait for SSH timeout, get re-tagged as broken, and repeat.

**Dependencies**: All other components

---

### 2. EventMonitor

**Purpose**: Monitor libvirt domain lifecycle events

**Responsibilities**:
- Register libvirt event callbacks
- Run libvirt event loop
- Dispatch events to user callbacks
- Handle event loop lifecycle

**Key Methods**:
- `start()`: Register callbacks and start event loop
- `stop()`: Deregister callbacks and stop event loop
- `_lifecycle_callback()`: Internal libvirt callback handler
- `_run_event_loop()`: Background event loop task

**Libvirt Integration**:
```python
# Register for all domain lifecycle events
conn.domainEventRegisterAny(
    None,  # All domains
    libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE,
    self._lifecycle_callback,
    None
)

# Events handled:
# - VIR_DOMAIN_EVENT_STARTED (0)
# - VIR_DOMAIN_EVENT_STOPPED (1)
```

**Dependencies**: libvirt connection

---

### 3. VMTracker

**Purpose**: Track active monitoring sessions and implement debouncing

**Responsibilities**:
- Maintain registry of active monitoring sessions
- Prevent duplicate monitoring (debouncing)
- Cancel all sessions on shutdown
- Thread-safe session management

**Data Structure**:
```python
@dataclass
class MonitorSession:
    vm_uuid: str          # Unique VM identifier
    vm_name: str          # Display name
    started_at: datetime  # When monitoring started
    task: asyncio.Task    # Async task doing the monitoring
```

**Debouncing Logic**:
```python
# First start for UUID "abc-123" → returns True
await tracker.start_monitoring("abc-123", "vm1", task1)

# Second start for same UUID → returns False (debounced)
await tracker.start_monitoring("abc-123", "vm1", task2)
```

**Thread Safety**: Uses `asyncio.Lock` for all registry operations

**Dependencies**: None (pure state management)

---

### 4. SSHChecker

**Purpose**: Verify SSH connectivity with retry logic

**Responsibilities**:
- Attempt SSH authentication (not just TCP connect)
- Retry on connection failures
- Fail immediately on authentication errors
- Respect timeout constraints

**Retry Strategy**:
```python
# Retry on these errors:
- ConnectionRefusedError
- ConnectionResetError  
- TimeoutError
- paramiko.SSHException

# Don't retry on:
- paramiko.AuthenticationException
- Missing auth credentials
```

**Configuration**:
```python
@dataclass
class SSHConfig:
    username: str
    key_path: Optional[str] = None
    password: Optional[str] = None
    port: int = 22
```

**Dependencies**: paramiko

---

### 5. TagCleaner

**Purpose**: Orchestrate the complete VM processing workflow

**Responsibilities**:
- Get VM IP address (with retry, skipping loopback addresses)
- Coordinate SSH checking with uptime verification
- Check `in_use` metadata before tag removal (prevents mid-run removal)
- Remove tags after SSH success + safety checks pass
- Mark VMs as broken on SSH timeout
- Run `--on-broken` external script when a VM is marked broken
- Handle errors gracefully

**Workflow**:
```
1. VM reboot/start event received
   ↓
2. Daemon checks VM has removable tags (e.g., 'used')
   ↓
3. Register with VMTracker (debouncing check)
   ↓
4. Get VM IP address (retry up to 10 times, skip 127.*)
   ↓
5. Wait for SSH with uptime < 120s (SSHChecker)
   ├─ timeout → 5b. Mark VM as broken (add 'broken' tag, keep 'used')
   │              → 5c. Run --on-broken script if configured (async, 60s timeout)
   │              → Stop monitoring
   └─ success ↓
6. Wait 5 seconds (let ansible-deployer finish cleanup)
   ↓
7. Check in_use metadata
   ├─ in_use=true → Skip tag removal (deployer still active)
   └─ in_use=false ↓
8. Remove specified tags
   ↓
9. Stop monitoring (VMTracker)
```

**Safety Checks** (prevents race conditions):
- **Tag pre-check** (step 2): VMs without removable tags are ignored, preventing orphaned monitors from `reset_vm()` reboots after tag removal
- **Uptime verification** (step 5): SSH must connect to a VM with uptime < 120 seconds, confirming it's a fresh boot and not a stale connection
- **Cleanup delay** (step 6): 5-second wait after SSH succeeds to let ansible-deployer's non-blocking `reset_vm()` + `mark_available()` complete
- **In-use check** (step 7): Reads VM metadata to verify no deployer session is active. Prevents removing the `used` tag during a playbook's own reboot cycle (e.g., OS install reboots the VM mid-run)

**IP Retry Logic**:
- **Problem**: VMs may not have IP immediately after start (DHCP lease renewal)
- **Solution**: Retry up to 10 times with 3s intervals (30s total)
- **Loopback skip**: Addresses matching `127.*` are ignored (QEMU guest agent may return loopback before real NIC is up)
- **Benefit**: Handles rapid VM restarts gracefully

**Broken VM Tagging**:
- **Problem**: Some VMs may never become SSH-accessible (PXE installer, broken OS, etc.)
- **Solution**: After `--max-wait-time` (default: 30 minutes), add a `--broken-tag` (default: `broken`) to the VM
- **Behavior**: The `used` tag is intentionally kept so the VM won't be reallocated. The `broken` tag provides visibility for external monitoring.
- **Auto-exclude**: The daemon auto-appends `broken_tag` to `exclude_tags`, so broken VMs won't be re-monitored on the next reboot or `--check-existing` scan. The ansible-deployer also auto-excludes `broken` VMs from allocation.
- **On-broken hook**: If `--on-broken /path/to/script.sh` is configured, the script is called asynchronously after tagging. It receives `VM_NAME`, `VM_UUID`, `VM_IP`, `VM_TAGS`, `VM_BROKEN_TAG`, `VM_WAIT_TIME`, and `LIBVIRT_URI` as environment variables. The script has a 60-second timeout and non-zero exits are logged as warnings.

**Dependencies**: SSHChecker, VMTracker, MetadataManager, vm_tools_common

---

## Data Flow

### Normal Flow: VM Reboot → Tag Removal

```
┌─────────┐
│ VM      │ 1. VM reboots (reset_vm or lifecycle event)
│ Reboots │
└────┬────┘
     │
     ▼
┌─────────────┐
│  libvirt    │ 2. VIR_DOMAIN_EVENT_ID_REBOOT
│   event     │    (or VIR_DOMAIN_EVENT_STARTED)
└──────┬──────┘
       │
       ▼
┌──────────────┐
│EventMonitor  │ 3. _reboot_callback() / _lifecycle_callback()
│              │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Daemon     │ 4. _handle_vm_started()
│              │    • Check tag filters
│              │    • Verify VM has removable tags (e.g., 'used')
└──────┬───────┘    • Skip if no removable tags (prevents orphaned monitors)
       │
       ▼
┌──────────────┐
│ TagCleaner   │ 5. handle_vm_started()
│              │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  VMTracker   │ 6. start_monitoring()
│              │    • Debouncing check
└──────┬───────┘    • Register session
       │
       ▼
┌──────────────┐
│ TagCleaner   │ 7. _monitor_vm()
│              │    • Get IP (retry, skip 127.*)
└──────┬───────┘    
       │
       ▼
┌──────────────┐
│ SSHChecker   │ 8. wait_for_ssh()
│              │    • Retry connection
│              │    • Full auth + uptime < 120s
│              │    • Returns "success" / "timeout" / "auth_failure"
└──────┬───────┘    
       │
       ├── timeout ──► 8b. _mark_vm_broken() (add 'broken' tag)
       │                    └──► 8c. _run_on_broken_script() (if --on-broken configured)
       │
       ▼ success
┌──────────────┐
│ TagCleaner   │ 9. Wait 5 seconds (let deployer finish cleanup)
│              │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ TagCleaner   │ 10. _is_vm_in_use() check
│              │     • Read in_use metadata
│              │     • Skip if deployer still active
└──────┬───────┘
       │
       ▼ in_use=false
┌──────────────┐
│ TagCleaner   │ 11. _remove_tags()
│              │     • Remove each tag
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  VMTracker   │ 12. stop_monitoring()
│              │     • Unregister session
└──────────────┘
```

### Debouncing Flow: VM Reboots

```
┌─────────┐
│ VM      │ 1. VM already being monitored
│ Reboots │    (SSH check in progress)
└────┬────┘
     │
     ▼
┌─────────────┐
│  libvirt    │ 2. VIR_DOMAIN_EVENT_STARTED
│   event     │    (from reboot)
└──────┬──────┘
       │
       ▼
┌──────────────┐
│EventMonitor  │ 3. _lifecycle_callback()
│              │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ TagCleaner   │ 4. handle_vm_started()
│              │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  VMTracker   │ 5. start_monitoring()
│              │    ✗ UUID already registered
└──────┬───────┘    → Returns False
       │
       ▼
┌──────────────┐
│ TagCleaner   │ 6. Cancel new task
│              │    (debounced!)
└──────────────┘

Original SSH check continues uninterrupted
```

---

## Key Design Decisions

### 1. Why Async/Await?

**Decision**: Use asyncio for concurrency

**Alternatives Considered**:
- Threading: More complex, GIL limitations
- Multiprocessing: Overkill for I/O-bound work

**Rationale**:
- VM monitoring is I/O-bound (SSH, network)
- asyncio provides lightweight concurrency
- Better resource efficiency than threads
- Easier to reason about (no race conditions)

### 2. Why Separate VMTracker?

**Decision**: Dedicated component for session tracking

**Alternative**: Track sessions in TagCleaner

**Rationale**:
- Single Responsibility Principle
- Easier to test debouncing logic in isolation
- Reusable component (could be used elsewhere)
- Clear separation of concerns

### 3. Why Retry IP Address Resolution?

**Decision**: Retry IP resolution up to 10 times (30s)

**Problem**: VMs don't always have IP immediately after start

**Context**: During rapid restarts, DHCP lease renewal takes time

**Rationale**:
- Handles real-world network delays
- Prevents false failures
- 30s total wait is reasonable for DHCP

### 4. Why Full SSH Authentication?

**Decision**: Perform complete SSH auth, not just TCP connect

**Alternative**: Only check if port 22 is open

**Rationale**:
- Port being open doesn't mean VM is ready
- SSH service may be starting (accepting connections but rejecting auth)
- Full auth confirms VM is truly operational
- User requirement: "SSH: Full authentication required"

### 5. Why Debouncing?

**Decision**: Ignore VM start events while already monitoring

**Problem**: VM reboots generate multiple start events

**Rationale**:
- Prevents duplicate SSH checks
- Reduces resource usage
- SSH check from first start is still valid
- More efficient than cancelling and restarting

### 6. Why Check `in_use` Metadata Before Tag Removal?

**Decision**: Read `in_use` metadata from ansible-deployer before removing `used` tag

**Problem**: A playbook may reboot the VM mid-run (e.g., OS install). vm-manager detects the reboot, waits for SSH (which succeeds on the freshly installed OS), and removes the `used` tag — while the deployer is still orchestrating the VM. This allows another deployer to allocate it concurrently.

**Alternatives Considered**:
- Only let ansible-deployer remove tags (requires `--mark-available` flag, currently not used)
- Session-based tag tracking (complex, requires shared state)

**Rationale**:
- Minimal code change (single metadata read before tag removal)
- Leverages existing `in_use` metadata that ansible-deployer already maintains
- Safe default: on error reading metadata, assumes not in use (avoids permanent tag retention)
- Validated with 10-run stress test (80 parallel deployer jobs)

### 7. Why Default SSH Timeout and Broken Tagging?

**Decision**: Default `--max-wait-time` to 1800s (30 min) and tag timed-out VMs as `broken`

**Problem**: `max_wait_time=None` (infinite) caused unbounded resource accumulation. VMs that could never SSH (PXE installer, broken OS) would accumulate monitor tasks indefinitely.

**Rationale**:
- 30 minutes covers even slow PXE install cycles (~8-9 minutes observed in production)
- `broken` tag provides visibility without removing `used` (VM stays reserved)
- External tooling can monitor for `broken` tags and alert operators
- Configurable: `--broken-tag`, `--no-broken-tag`, `--max-wait-time`

### 8. Why Auto-Exclude Broken VMs?

**Decision**: Daemon auto-appends `broken_tag` to `exclude_tags`; deployer auto-appends `"broken"` to `exclude_tags`

**Problem**: Without auto-exclude, the daemon would re-monitor broken VMs on every reboot or `--check-existing` scan, wait 30 minutes for SSH timeout, re-add the `broken` tag, and repeat forever. Similarly, the deployer could allocate a broken VM that can't be reached via SSH.

**Rationale**:
- Zero-configuration: operators don't need to manually add `--exclude-tag broken` to every deployment
- Prevents infinite monitoring loops in the daemon
- The `used` tag is kept on broken VMs as a secondary safety net, but auto-exclude provides the primary protection
- Both daemon and deployer copy the `exclude_tags` list before modifying to avoid mutating the caller

### 9. Why an External Script Hook for Broken VMs?

**Decision**: Optional `--on-broken /path/to/script.sh` that runs when a VM is marked broken

**Alternatives Considered**:
- Built-in webhook/HTTP support (too opinionated, adds dependencies)
- Built-in email/Slack integration (too specific, maintenance burden)
- Log-only approach (requires external log parsing, delayed detection)

**Rationale**:
- Maximum flexibility: operators can integrate with any system (alerting, ticketing, auto-remediation)
- Minimal code: ~30 lines in `tag_cleaner.py`, no new dependencies
- Standard interface: environment variables are universally accessible from any language
- Safe defaults: script failures don't affect vm-manager operation
- 60-second timeout prevents hung scripts from blocking the daemon

---

## Error Handling

### Error Categories

#### 1. **Transient Errors** (Retry)

Errors that are expected to be temporary:

```python
# SSH connection failures
ConnectionRefusedError    # SSH not ready yet
TimeoutError             # Network slow
paramiko.SSHException    # SSH handshake failed

# IP resolution
None                     # DHCP lease pending
VMNotFoundException      # VM state transitioning
```

**Strategy**: Retry with exponential backoff or fixed intervals

#### 2. **Permanent Errors** (Fail Fast)

Errors that won't be fixed by retrying:

```python
# SSH authentication
paramiko.AuthenticationException  # Wrong credentials

# Configuration
FileNotFoundError        # SSH key missing
PermissionError         # Can't read key file
```

**Strategy**: Log error, stop processing this VM, continue daemon

#### 3. **Fatal Errors** (Shutdown)

Errors that require daemon shutdown:

```python
# Libvirt connection
libvirt.libvirtError     # Connection lost
```

**Strategy**: Log error, initiate graceful shutdown

### Error Handling Patterns

#### Pattern 1: Retry with Timeout

```python
async def wait_for_ssh(self, hostname, vm_name):
    start_time = datetime.now()
    attempt = 0
    
    while True:
        attempt += 1
        
        # Check timeout
        if self.max_wait_time:
            elapsed = (datetime.now() - start_time).total_seconds()
            if elapsed >= self.max_wait_time:
                logger.warning(f"SSH timeout for {vm_name}")
                return False
        
        # Try connection
        result = await self._try_ssh_connect(hostname, vm_name, attempt)
        
        if result == "success":
            return True
        elif result == "auth_failure":
            return False  # Don't retry auth failures
        
        # Retry after interval
        await asyncio.sleep(self.check_interval)
```

#### Pattern 2: Best Effort

```python
async def _remove_tags(self, domain, vm_name, vm_uuid):
    """Remove tags with best effort (continue even if one fails)."""
    for tag in self.tags_to_remove:
        try:
            await loop.run_in_executor(
                None,
                remove_vm_tag,
                self.conn,
                domain,
                tag
            )
            logger.info(f"Removed tag '{tag}' from {vm_name}")
        except Exception as e:
            # Log but continue with other tags
            logger.error(f"Failed to remove tag '{tag}': {e}")
            # Don't raise - try other tags
```

#### Pattern 3: User-Friendly Messages

```python
try:
    conn = libvirt.open(uri)
except libvirt.libvirtError as e:
    if "polkit" in str(e).lower():
        raise RuntimeError(
            "Libvirt authentication failed. Run with sudo or add user to libvirt group."
        ) from e
    else:
        raise RuntimeError(f"Failed to connect to libvirt: {e}") from e
```

---

## Concurrency Model

### AsyncIO Event Loop

```
┌────────────────────────────────────────┐
│        AsyncIO Event Loop              │
│                                        │
│  ┌──────────────┐  ┌──────────────┐  │
│  │ Event Loop   │  │ Event Loop   │  │
│  │   (libvirt)  │  │  (asyncio)   │  │
│  └──────┬───────┘  └───────┬──────┘  │
│         │                   │          │
│         ▼                   ▼          │
│  ┌──────────────────────────────────┐ │
│  │     Multiple Concurrent Tasks     │ │
│  │                                   │ │
│  │  Task 1: Monitor VM-1            │ │
│  │  Task 2: Monitor VM-2            │ │
│  │  Task 3: Monitor VM-3            │ │
│  │  Task 4: Continuous boot loop    │ │
│  │  Task 5: Event loop runner       │ │
│  └──────────────────────────────────┘ │
└────────────────────────────────────────┘
```

### Task Management

Each monitored VM gets its own asyncio Task:

```python
# Task created when VM starts
task = asyncio.create_task(
    self._monitor_vm(domain, vm_uuid, vm_name)
)

# Task registered with tracker
await self.vm_tracker.start_monitoring(vm_uuid, vm_name, task)

# Task automatically cleaned up when done
# - SSH succeeds → tag removed → task completes
# - SSH fails → task completes
# - Timeout → task completes
# - Daemon shutdown → task cancelled
```

### Synchronization

Only one lock used for thread safety:

```python
class VMTracker:
    def __init__(self):
        self._sessions: Dict[str, MonitorSession] = {}
        self._lock = asyncio.Lock()  # Protects _sessions dict
    
    async def start_monitoring(self, vm_uuid, vm_name, task):
        async with self._lock:
            if vm_uuid in self._sessions:
                return False  # Already monitoring
            
            self._sessions[vm_uuid] = MonitorSession(...)
            return True
```

### Blocking Operations

Blocking I/O runs in thread pool executor:

```python
# SSH connection (paramiko is synchronous)
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(
    None,  # Default thread pool
    self._ssh_connect_sync,
    hostname,
    vm_name,
    attempt
)

# Tag removal (libvirt is synchronous)
await loop.run_in_executor(
    None,
    remove_vm_tag,
    self.conn,
    domain,
    tag
)
```

---

## Shared Library (vm_tools_common)

Components shared with `ansible-deployer`:

```
vm_tools_common/
├── __init__.py
├── exceptions.py           # VMNotFoundException, etc.
├── vm_operations.py        # get_vm_ip, add_vm_tag, remove_vm_tag
├── libvirt_connection.py   # LibvirtConnection class
└── tag_filters.py          # vm_matches_tags
```

### Benefits of Sharing

- **Code Reuse**: Both tools use same VM operations
- **Consistency**: Tag format identical between tools
- **Maintainability**: Bug fixes benefit both tools
- **Testing**: Shared code tested by both test suites

### Dependency Diagram

```
┌──────────────────┐         ┌──────────────────┐
│ ansible-deployer │         │   vm-manager     │
└────────┬─────────┘         └────────┬─────────┘
         │                            │
         └────────────┬───────────────┘
                      │
                      ▼
            ┌─────────────────┐
            │ vm_tools_common │
            │                 │
            │ • VM operations │
            │ • Tag filters   │
            │ • Exceptions    │
            │ • Libvirt conn  │
            └─────────────────┘
```

---

## Performance Characteristics

### Memory Usage

- **Baseline**: ~40 MB (daemon + event loop)
- **Per VM**: ~5 MB (task + session state)
- **30 VMs**: ~190 MB total

### CPU Usage

- **Idle**: <1% (event loop polling)
- **Event processing**: 1-3% spike per event
- **SSH checking**: 2-5% per concurrent check

### Network

- **Minimal**: Only SSH connections
- **Bandwidth**: <1 KB/s per VM (SSH handshake)

### Scalability

- **Tested**: 30+ concurrent VMs
- **Theoretical**: 100s (limited by system resources)
- **Bottlenecks**:
  - SSH connection timeouts
  - Thread pool size (default executor)
  - libvirt event processing

---

## Future Enhancements

Potential improvements (not currently implemented):

1. **Metrics & Monitoring**
   - Prometheus metrics endpoint
   - Grafana dashboards
   - VM processing statistics

2. **Advanced Retry Strategies**
   - Exponential backoff for SSH
   - Configurable retry policies
   - Circuit breaker pattern

3. **Webhook Integration**
   - HTTP callbacks on tag removal
   - Slack/Discord notifications
   - Integration with external systems

4. **Multiple Tag Operations**
   - Add tags on SSH success
   - Tag transformations
   - Conditional tag operations

5. **Database Backend**
   - Persistent session state
   - Historical metrics
   - Audit logging

---

## References

- [Main README](README.md) - User documentation
- [Testing Documentation](TESTING.md) - Test suite details
- [Python asyncio](https://docs.python.org/3/library/asyncio.html)
- [libvirt events](https://libvirt.org/api.html#event)
- [paramiko](https://www.paramiko.org/)
