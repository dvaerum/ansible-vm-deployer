"""
Tests for ansible-deployer signal handling and subprocess cleanup.

Covers:
- SIGTERM handler installation and behavior
- Subprocess registration, deregistration, and termination
- Executor subprocess cleanup on normal exit and signals
- End-to-end: finally block runs on SIGTERM and KeyboardInterrupt
"""

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest
from unittest.mock import Mock, patch

from ansible_deployer import signal_handler


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_signal_state():
    """Reset module state and signal handlers before/after each test.

    Saves and restores both SIGTERM and SIGINT handlers so tests
    don't leak signal state (e.g., block_further_signals() sets
    both to SIG_IGN).
    """
    original_sigterm = signal.getsignal(signal.SIGTERM)
    original_sigint = signal.getsignal(signal.SIGINT)
    signal_handler._active_process = None
    yield
    signal_handler._active_process = None
    signal.signal(signal.SIGTERM, original_sigterm)
    signal.signal(signal.SIGINT, original_sigint)


# ── TestInstallSignalHandlers ─────────────────────────────────────────


class TestInstallSignalHandlers:
    """Tests for install_signal_handlers()."""

    def test_installs_sigterm_handler(self):
        """After install, SIGTERM handler is our custom handler."""
        signal_handler.install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        assert handler is signal_handler._sigterm_handler

    def test_sigint_handler_unchanged(self):
        """install_signal_handlers does NOT replace SIGINT handler."""
        original = signal.getsignal(signal.SIGINT)
        signal_handler.install_signal_handlers()
        assert signal.getsignal(signal.SIGINT) is original


# ── TestBlockFurtherSignals ───────────────────────────────────────────


class TestBlockFurtherSignals:
    """Tests for block_further_signals() — protects cleanup from
    a second SIGTERM or Ctrl+C."""

    def test_sigterm_ignored_after_block(self):
        """After block_further_signals(), SIGTERM is SIG_IGN."""
        signal_handler.block_further_signals()
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN

    def test_sigint_ignored_after_block(self):
        """After block_further_signals(), SIGINT is SIG_IGN."""
        signal_handler.block_further_signals()
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN

    def test_second_sigterm_does_not_raise(self):
        """After block_further_signals(), SIGTERM does not raise."""
        signal_handler.block_further_signals()
        # This should be silently ignored, not raise SystemExit
        os.kill(os.getpid(), signal.SIGTERM)

    def test_cleanup_completes_despite_second_sigterm(self):
        """Simulate the cli.py finally block: block signals, then
        verify cleanup code runs to completion even if SIGTERM arrives
        mid-cleanup."""
        signal_handler.install_signal_handlers()
        cleanup_steps = []

        try:
            raise SystemExit(143)  # Simulate first SIGTERM
        except SystemExit:
            pass
        finally:
            signal_handler.block_further_signals()
            cleanup_steps.append("terminate_subprocess")
            # Second SIGTERM arrives mid-cleanup
            os.kill(os.getpid(), signal.SIGTERM)
            cleanup_steps.append("reset_vm")
            os.kill(os.getpid(), signal.SIGTERM)
            cleanup_steps.append("mark_available")

        assert cleanup_steps == [
            "terminate_subprocess",
            "reset_vm",
            "mark_available",
        ], f"Cleanup was interrupted: {cleanup_steps}"


# ── TestSubprocessRegistration ────────────────────────────────────────


class TestSubprocessRegistration:
    """Tests for register/deregister/terminate subprocess."""

    def test_register_stores_process(self):
        """register_subprocess stores the process reference."""
        proc = Mock(spec=subprocess.Popen)
        signal_handler.register_subprocess(proc)
        assert signal_handler._active_process is proc

    def test_deregister_clears_process(self):
        """deregister_subprocess clears the stored reference."""
        proc = Mock(spec=subprocess.Popen)
        signal_handler.register_subprocess(proc)
        signal_handler.deregister_subprocess()
        assert signal_handler._active_process is None

    def test_terminate_calls_terminate_on_running_process(self):
        """terminate_active_subprocess sends SIGTERM to running child."""
        proc = Mock(spec=subprocess.Popen)
        proc.poll.return_value = None  # Still running
        proc.pid = 12345
        signal_handler.register_subprocess(proc)

        signal_handler.terminate_active_subprocess()

        proc.terminate.assert_called_once()

    def test_terminate_noop_when_process_already_exited(self):
        """terminate_active_subprocess is a no-op if child already exited."""
        proc = Mock(spec=subprocess.Popen)
        proc.poll.return_value = 0  # Already exited
        signal_handler.register_subprocess(proc)

        signal_handler.terminate_active_subprocess()

        proc.terminate.assert_not_called()

    def test_terminate_noop_when_no_process_registered(self):
        """terminate_active_subprocess is a no-op with no registered process."""
        # Should not raise
        signal_handler.terminate_active_subprocess()

    def test_terminate_handles_oserror(self):
        """terminate_active_subprocess swallows OSError from terminate()."""
        proc = Mock(spec=subprocess.Popen)
        proc.poll.return_value = None
        proc.pid = 12345
        proc.terminate.side_effect = OSError("No such process")
        signal_handler.register_subprocess(proc)

        # Should not raise
        signal_handler.terminate_active_subprocess()


# ── TestSigtermHandler ────────────────────────────────────────────────


class TestSigtermHandler:
    """Tests for the SIGTERM signal handler function."""

    def test_raises_system_exit_143(self):
        """SIGTERM handler raises SystemExit with code 143 (128+15)."""
        with pytest.raises(SystemExit) as exc_info:
            signal_handler._sigterm_handler(
                signal.SIGTERM, None
            )
        assert exc_info.value.code == 143

    def test_terminates_subprocess_before_exit(self):
        """SIGTERM handler terminates child process before raising."""
        proc = Mock(spec=subprocess.Popen)
        proc.poll.return_value = None
        proc.pid = 12345
        signal_handler.register_subprocess(proc)

        with pytest.raises(SystemExit):
            signal_handler._sigterm_handler(
                signal.SIGTERM, None
            )

        proc.terminate.assert_called_once()

    def test_sigterm_triggers_finally_blocks(self):
        """SystemExit from SIGTERM handler triggers finally blocks."""
        signal_handler.install_signal_handlers()
        finally_ran = False

        try:
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            finally:
                finally_ran = True
        except SystemExit:
            pass

        assert finally_ran, "finally block did not run after SIGTERM"

    def test_sigterm_not_caught_by_except_exception(self):
        """SystemExit from SIGTERM is NOT caught by except Exception."""
        signal_handler.install_signal_handlers()
        caught_by_exception = False

        try:
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except Exception:
                caught_by_exception = True
        except SystemExit:
            pass

        assert not caught_by_exception, (
            "SystemExit should not be caught by except Exception"
        )


# ── TestKeyboardInterruptCleanup ──────────────────────────────────────


class TestKeyboardInterruptCleanup:
    """Tests for KeyboardInterrupt (SIGINT / Ctrl+C) behavior."""

    def test_keyboard_interrupt_triggers_finally(self):
        """KeyboardInterrupt triggers finally blocks (Python default)."""
        finally_ran = False

        try:
            try:
                raise KeyboardInterrupt()
            finally:
                finally_ran = True
        except KeyboardInterrupt:
            pass

        assert finally_ran

    def test_keyboard_interrupt_not_caught_by_except_exception(self):
        """KeyboardInterrupt is NOT caught by except Exception."""
        caught = False
        try:
            try:
                raise KeyboardInterrupt()
            except Exception:
                caught = True
        except KeyboardInterrupt:
            pass

        assert not caught


# ── TestExecutorSubprocessCleanup ─────────────────────────────────────


class TestExecutorSubprocessCleanup:
    """Tests for subprocess lifecycle in AnsibleExecutor."""

    def test_executor_registers_subprocess(self, tmp_path):
        """Executor registers subprocess with signal_handler after Popen."""
        from ansible_deployer.ansible_executor import AnsibleExecutor

        # Create a minimal playbook file (executor checks existence)
        playbook = tmp_path / "test.yml"
        playbook.write_text("---\n- hosts: all\n")

        executor = AnsibleExecutor(log_dir=tmp_path)

        registered = []
        original_register = signal_handler.register_subprocess

        def spy_register(proc):
            registered.append(proc)
            original_register(proc)

        with patch.object(
            signal_handler, "register_subprocess", side_effect=spy_register
        ), patch(
            "subprocess.Popen"
        ) as mock_popen, patch(
            "shutil.which", return_value="/usr/bin/ansible-playbook"
        ):
            mock_proc = Mock()
            mock_proc.stdout = iter(["line1\n"])
            mock_proc.wait.return_value = 0
            mock_proc.poll.return_value = 0
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc

            executor.execute_playbook(
                playbook_path=playbook,
                task_id="test_task",
            )

        assert len(registered) == 1
        assert registered[0] is mock_proc

    def test_executor_deregisters_on_success(self, tmp_path):
        """Executor deregisters subprocess after successful completion."""
        from ansible_deployer.ansible_executor import AnsibleExecutor

        playbook = tmp_path / "test.yml"
        playbook.write_text("---\n- hosts: all\n")

        executor = AnsibleExecutor(log_dir=tmp_path)

        with patch(
            "subprocess.Popen"
        ) as mock_popen, patch(
            "shutil.which", return_value="/usr/bin/ansible-playbook"
        ):
            mock_proc = Mock()
            mock_proc.stdout = iter(["ok\n"])
            mock_proc.wait.return_value = 0
            mock_proc.poll.return_value = 0
            mock_proc.returncode = 0
            mock_popen.return_value = mock_proc

            executor.execute_playbook(
                playbook_path=playbook,
                task_id="test_task",
            )

        assert signal_handler._active_process is None

    def test_executor_terminates_subprocess_on_exception(self, tmp_path):
        """Executor terminates subprocess if exception occurs during read."""
        from ansible_deployer.ansible_executor import (
            AnsibleExecutor, AnsibleExecutionError,
        )

        playbook = tmp_path / "test.yml"
        playbook.write_text("---\n- hosts: all\n")

        executor = AnsibleExecutor(log_dir=tmp_path)

        def exploding_iterator():
            yield "line1\n"
            raise RuntimeError("Simulated failure")

        with patch(
            "subprocess.Popen"
        ) as mock_popen, patch(
            "shutil.which", return_value="/usr/bin/ansible-playbook"
        ):
            mock_proc = Mock()
            mock_proc.stdout = exploding_iterator()
            mock_proc.wait.return_value = 1
            mock_proc.poll.return_value = None  # Still running
            mock_proc.pid = 99999
            mock_proc.returncode = None
            mock_popen.return_value = mock_proc

            with pytest.raises(AnsibleExecutionError):
                executor.execute_playbook(
                    playbook_path=playbook,
                    task_id="test_task",
                )

        # Subprocess should have been terminated in finally block
        mock_proc.terminate.assert_called()
        # Active process should be cleared
        assert signal_handler._active_process is None

    def test_executor_kills_subprocess_after_timeout(self, tmp_path):
        """If subprocess ignores SIGTERM, executor escalates to SIGKILL
        after 5 second timeout."""
        from ansible_deployer.ansible_executor import (
            AnsibleExecutor, AnsibleExecutionError,
        )

        playbook = tmp_path / "test.yml"
        playbook.write_text("---\n- hosts: all\n")

        executor = AnsibleExecutor(log_dir=tmp_path)

        def exploding_iterator():
            yield "line1\n"
            raise RuntimeError("Simulated failure")

        def wait_side_effect(timeout=None):
            """Simulate: wait(timeout=5) times out, wait() after kill
            returns exit code."""
            if timeout is not None:
                raise subprocess.TimeoutExpired(
                    cmd="ansible-playbook", timeout=timeout
                )
            return -9  # Killed by SIGKILL

        with patch(
            "subprocess.Popen"
        ) as mock_popen, patch(
            "shutil.which", return_value="/usr/bin/ansible-playbook"
        ):
            mock_proc = Mock()
            mock_proc.stdout = exploding_iterator()
            mock_proc.pid = 99999
            mock_proc.returncode = None
            # poll() returns None = still running (even after terminate)
            mock_proc.poll.return_value = None
            mock_proc.wait.side_effect = wait_side_effect
            mock_popen.return_value = mock_proc

            with pytest.raises(AnsibleExecutionError):
                executor.execute_playbook(
                    playbook_path=playbook,
                    task_id="test_task",
                )

        # terminate() called by signal_handler.terminate_active_subprocess
        mock_proc.terminate.assert_called()
        # kill() called after wait(timeout=5) raised TimeoutExpired
        mock_proc.kill.assert_called_once()
        # wait() called again after kill() to reap the zombie
        # (at least one call with timeout and one without)
        wait_calls = mock_proc.wait.call_args_list
        has_timeout_call = any(
            c.kwargs.get("timeout") is not None for c in wait_calls
        )
        has_reap_call = any(
            c.kwargs.get("timeout") is None and c.args == ()
            for c in wait_calls
        )
        assert has_timeout_call, "Expected wait(timeout=5) call"
        assert has_reap_call, "Expected wait() call after kill()"


# ── TestEndToEndSignalCleanup ─────────────────────────────────────────


class TestEndToEndSignalCleanup:
    """End-to-end tests verifying cleanup runs on process termination.

    These tests spawn a real subprocess running a Python script that
    simulates the deploy command's try/finally structure, then send
    signals and verify the finally block executed.
    """

    @staticmethod
    def _run_signal_test_script(
        signal_to_send: str, install_handler: bool = True
    ) -> subprocess.CompletedProcess:
        """Run a helper script that simulates deploy and receives a signal.

        The script writes "FINALLY_RAN" to a file if the finally block
        executes. Returns the CompletedProcess result.
        """
        script = textwrap.dedent(f"""\
            import os
            import signal
            import sys
            import time
            import tempfile

            # Simulate the signal_handler module
            sys.path.insert(
                0,
                os.path.join(
                    os.path.dirname(__file__), "..", "src"
                ),
            )
            from ansible_deployer import signal_handler

            marker = sys.argv[1]

            if {install_handler!r}:
                signal_handler.install_signal_handlers()

            try:
                # Signal the parent that we're ready
                with open(marker + ".ready", "w") as f:
                    f.write("ready")

                # Block until signal arrives
                time.sleep(30)
            except (SystemExit, KeyboardInterrupt):
                pass
            finally:
                with open(marker + ".finally", "w") as f:
                    f.write("FINALLY_RAN")
        """)

        import tempfile
        marker = tempfile.mktemp()

        # Write the test script
        script_file = marker + ".py"
        with open(script_file, "w") as f:
            f.write(script)

        try:
            proc = subprocess.Popen(
                [sys.executable, script_file, marker],
                cwd=os.path.dirname(os.path.dirname(__file__)),
            )

            # Wait for the child to be ready
            ready_file = marker + ".ready"
            for _ in range(50):
                if os.path.exists(ready_file):
                    break
                time.sleep(0.1)
            else:
                proc.kill()
                proc.wait()
                pytest.fail("Child process never became ready")

            # Small delay to ensure the child is in time.sleep()
            time.sleep(0.1)

            # Send the signal
            sig = getattr(signal, signal_to_send)
            proc.send_signal(sig)
            proc.wait(timeout=10)

            # Check if finally block ran
            finally_file = marker + ".finally"
            finally_ran = os.path.exists(finally_file)
            finally_content = ""
            if finally_ran:
                with open(finally_file) as f:
                    finally_content = f.read()

            return subprocess.CompletedProcess(
                args=proc.args,
                returncode=proc.returncode,
                stdout=finally_content,
                stderr="",
            )

        finally:
            # Cleanup temp files
            for suffix in [".py", ".ready", ".finally"]:
                path = marker + suffix
                if os.path.exists(path):
                    os.unlink(path)

    def test_sigterm_with_handler_runs_finally(self):
        """SIGTERM with installed handler runs the finally block."""
        result = self._run_signal_test_script(
            "SIGTERM", install_handler=True
        )
        assert result.stdout == "FINALLY_RAN", (
            f"finally block did not run (exit code={result.returncode})"
        )

    def test_sigterm_without_handler_skips_finally(self):
        """SIGTERM without handler does NOT run the finally block."""
        result = self._run_signal_test_script(
            "SIGTERM", install_handler=False
        )
        assert result.stdout != "FINALLY_RAN", (
            "finally block should NOT run without signal handler "
            "(this proves the handler is needed)"
        )

    def test_sigint_runs_finally(self):
        """SIGINT (KeyboardInterrupt) runs the finally block."""
        result = self._run_signal_test_script(
            "SIGINT", install_handler=False
        )
        assert result.stdout == "FINALLY_RAN", (
            f"finally block did not run on SIGINT "
            f"(exit code={result.returncode})"
        )

    def test_second_sigterm_during_cleanup_does_not_interrupt(self):
        """A second SIGTERM during the finally block does NOT abort
        cleanup, because block_further_signals() ignores it."""
        script = textwrap.dedent("""\
            import os
            import signal
            import sys
            import time

            sys.path.insert(
                0,
                os.path.join(
                    os.path.dirname(__file__), "..", "src"
                ),
            )
            from ansible_deployer import signal_handler

            marker = sys.argv[1]
            signal_handler.install_signal_handlers()

            try:
                with open(marker + ".ready", "w") as f:
                    f.write("ready")
                time.sleep(30)
            except (SystemExit, KeyboardInterrupt):
                pass
            finally:
                signal_handler.block_further_signals()
                steps = []
                steps.append("step1")
                # Second SIGTERM arrives mid-cleanup
                os.kill(os.getpid(), signal.SIGTERM)
                steps.append("step2")
                # Third SIGTERM for good measure
                os.kill(os.getpid(), signal.SIGTERM)
                steps.append("step3")
                with open(marker + ".finally", "w") as f:
                    f.write(",".join(steps))
        """)

        import tempfile
        marker = tempfile.mktemp()
        script_file = marker + ".py"
        with open(script_file, "w") as f:
            f.write(script)

        try:
            proc = subprocess.Popen(
                [sys.executable, script_file, marker],
                cwd=os.path.dirname(os.path.dirname(__file__)),
            )

            ready_file = marker + ".ready"
            for _ in range(50):
                if os.path.exists(ready_file):
                    break
                time.sleep(0.1)
            else:
                proc.kill()
                proc.wait()
                pytest.fail("Child process never became ready")

            time.sleep(0.1)
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)

            finally_file = marker + ".finally"
            assert os.path.exists(finally_file), (
                "finally block did not run"
            )
            with open(finally_file) as f:
                steps = f.read()
            assert steps == "step1,step2,step3", (
                f"Cleanup was interrupted by second SIGTERM: {steps}"
            )

        finally:
            for suffix in [".py", ".ready", ".finally"]:
                path = marker + suffix
                if os.path.exists(path):
                    os.unlink(path)


class TestEndToEndSubprocessCleanup:
    """End-to-end test that a child process is terminated on SIGTERM."""

    def test_sigterm_terminates_child_subprocess(self):
        """When deploy gets SIGTERM, the ansible-playbook child is killed."""
        script = textwrap.dedent("""\
            import os
            import signal
            import subprocess
            import sys
            import time

            sys.path.insert(
                0,
                os.path.join(
                    os.path.dirname(__file__), "..", "src"
                ),
            )
            from ansible_deployer import signal_handler

            marker = sys.argv[1]
            signal_handler.install_signal_handlers()

            # Spawn a long-running child (simulates ansible-playbook)
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(300)"]
            )
            signal_handler.register_subprocess(child)

            # Write child PID so parent can check it
            with open(marker + ".child_pid", "w") as f:
                f.write(str(child.pid))

            # Signal ready
            with open(marker + ".ready", "w") as f:
                f.write("ready")

            try:
                child.wait()
            except (SystemExit, KeyboardInterrupt):
                pass
            finally:
                # Ensure child is terminated in finally block
                signal_handler.terminate_active_subprocess()
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
                with open(marker + ".finally", "w") as f:
                    f.write(str(child.poll() is not None))
        """)

        import tempfile
        marker = tempfile.mktemp()
        script_file = marker + ".py"
        with open(script_file, "w") as f:
            f.write(script)

        try:
            proc = subprocess.Popen(
                [sys.executable, script_file, marker],
                cwd=os.path.dirname(os.path.dirname(__file__)),
            )

            # Wait for ready
            ready_file = marker + ".ready"
            for _ in range(50):
                if os.path.exists(ready_file):
                    break
                time.sleep(0.1)
            else:
                proc.kill()
                proc.wait()
                pytest.fail("Child process never became ready")

            time.sleep(0.1)

            # Read child PID
            with open(marker + ".child_pid") as f:
                child_pid = int(f.read().strip())

            # Send SIGTERM to the parent
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)

            # Verify child process was terminated
            finally_file = marker + ".finally"
            assert os.path.exists(finally_file), (
                "finally block did not run"
            )
            with open(finally_file) as f:
                child_exited = f.read()
            assert child_exited == "True", (
                "Child subprocess was not terminated"
            )

            # Double-check: child PID should no longer be running
            try:
                os.kill(child_pid, 0)
                pytest.fail(
                    f"Child process {child_pid} is still running"
                )
            except ProcessLookupError:
                pass  # Expected: child is gone
            except PermissionError:
                pass  # Process exists but we can't signal it (unlikely)

        finally:
            for suffix in [
                ".py", ".ready", ".finally", ".child_pid"
            ]:
                path = marker + suffix
                if os.path.exists(path):
                    os.unlink(path)
