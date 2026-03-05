"""
Signal handling for graceful shutdown of ansible-deployer.

Converts SIGTERM into SystemExit so that try/finally cleanup blocks
run (VM reset, mark_available, tag removal). Without this, Python's
default SIGTERM handler kills the process immediately — skipping all
finally blocks and leaving VMs marked as in-use.

Also tracks the active ansible-playbook subprocess so it can be
terminated when the deployer receives a signal. This prevents orphan
child processes.

Usage:
    Call install_signal_handlers() once at the start of the deploy
    command. Call register_subprocess() / deregister_subprocess()
    around subprocess.Popen() calls.
"""

import os
import signal
import subprocess
import sys
from typing import Optional

# Module-level reference to the active child process.
# Only one playbook subprocess runs at a time.
_active_process: Optional[subprocess.Popen] = None


def _safe_stderr(msg: str) -> None:
    """Write a message to stderr without using the logging module.

    Signal handlers must not use logging (it acquires locks that may
    already be held by the interrupted code, causing a deadlock).
    os.write() to fd 2 is async-signal-safe.
    """
    try:
        os.write(sys.stderr.fileno(), msg.encode())
    except OSError:
        pass


def install_signal_handlers() -> None:
    """Install signal handlers for graceful shutdown.

    - SIGTERM: converted to SystemExit(143) so finally blocks run.
      Exit code 143 = 128 + 15 (SIGTERM), following Unix convention.
    - SIGINT: left as default (raises KeyboardInterrupt, which also
      triggers finally blocks). The OS sends SIGINT to the entire
      process group, so the child process gets it too.
    """
    signal.signal(signal.SIGTERM, _sigterm_handler)


def block_further_signals() -> None:
    """Ignore SIGTERM so that cleanup cannot be interrupted.

    Call this at the start of the finally block (cleanup phase).
    A second SIGTERM (e.g., Jenkins sending SIGTERM twice before
    SIGKILL) would otherwise raise SystemExit again, interrupting
    the VM reset and mark_available calls mid-way.

    SIGINT is also ignored during cleanup — if the user presses
    Ctrl+C while VMs are being reset, the cleanup must finish.
    """
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def register_subprocess(process: subprocess.Popen) -> None:
    """Register the active child process for cleanup on signal.

    Call this immediately after subprocess.Popen(). The registered
    process will be terminated if SIGTERM arrives before it exits.

    Args:
        process: The subprocess.Popen instance to track
    """
    global _active_process
    _active_process = process


def deregister_subprocess() -> None:
    """Clear the active child process reference.

    Call this after the subprocess has exited (process.wait()).
    """
    global _active_process
    _active_process = None


def terminate_active_subprocess() -> None:
    """Terminate the active child process if one is running.

    Sends SIGTERM to the child. Safe to call even if no subprocess
    is registered or it has already exited.

    Uses os.write() instead of logging because this may be called
    from a signal handler where logging locks could deadlock.
    """
    proc = _active_process
    if proc is not None and proc.poll() is None:
        _safe_stderr(
            f"Terminating child process (pid={proc.pid})\n"
        )
        try:
            proc.terminate()
        except OSError:
            pass


def _sigterm_handler(signum: int, frame) -> None:
    """Handle SIGTERM: terminate child process and raise SystemExit.

    SystemExit inherits from BaseException (not Exception), so it
    won't be caught by ``except Exception`` blocks but WILL trigger
    finally blocks — exactly what we need for VM cleanup.
    """
    _safe_stderr(
        "Received SIGTERM, initiating graceful shutdown\n"
    )
    terminate_active_subprocess()
    raise SystemExit(128 + signal.SIGTERM)
