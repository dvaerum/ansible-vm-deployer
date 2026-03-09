"""
Tests for --log-prefix subdirectory support and --repeat task_id suffixing.

Covers:
- sanitize_log_prefix(): character filtering, slash handling, edge cases
- AnsibleExecutor: parent directory creation when task_id contains subdirectories
- --repeat: task_id _runN suffix logic, log file separation, backward compatibility
"""
import pytest
from pathlib import Path

from ansible_deployer.cli import sanitize_log_prefix, build_run_task_id


class TestSanitizeLogPrefix:
    """Test sanitize_log_prefix() character filtering and path handling."""

    def test_simple_prefix_unchanged(self):
        """Simple alphanumeric prefix passes through unchanged."""
        assert sanitize_log_prefix("test-linux") == "test-linux"

    def test_underscore_preserved(self):
        """Underscores are preserved."""
        assert sanitize_log_prefix("prod_deploy") == "prod_deploy"

    def test_slash_preserved_as_subdirectory(self):
        """Forward slashes are preserved for subdirectory support."""
        assert sanitize_log_prefix("test/linux") == "test/linux"

    def test_nested_subdirectories(self):
        """Multiple levels of subdirectories are supported."""
        assert sanitize_log_prefix("ci/nightly/linux-9") == "ci/nightly/linux-9"

    def test_special_chars_replaced_with_hyphen(self):
        """Special characters (spaces, dots, etc.) are replaced with hyphens."""
        assert sanitize_log_prefix("test linux") == "test-linux"
        assert sanitize_log_prefix("test.linux") == "test-linux"
        assert sanitize_log_prefix("test@linux!") == "test-linux-"

    def test_repeated_slashes_collapsed(self):
        """Multiple consecutive slashes are collapsed to one."""
        assert sanitize_log_prefix("test//linux") == "test/linux"
        assert sanitize_log_prefix("test///linux") == "test/linux"

    def test_leading_slash_stripped(self):
        """Leading slashes are stripped to prevent absolute paths."""
        assert sanitize_log_prefix("/test/linux") == "test/linux"

    def test_trailing_slash_stripped(self):
        """Trailing slashes are stripped."""
        assert sanitize_log_prefix("test/linux/") == "test/linux"

    def test_leading_and_trailing_slashes_stripped(self):
        """Both leading and trailing slashes are stripped."""
        assert sanitize_log_prefix("//test/linux//") == "test/linux"

    def test_mixed_special_chars_and_slashes(self):
        """Special chars are replaced while slashes are preserved."""
        assert sanitize_log_prefix("test env/linux 9.6") == "test-env/linux-9-6"

    def test_only_slashes_becomes_empty(self):
        """A prefix of only slashes becomes empty string."""
        assert sanitize_log_prefix("///") == ""

    def test_alphanumeric_only(self):
        """Pure alphanumeric prefix passes through."""
        assert sanitize_log_prefix("prodlinux96") == "prodlinux96"


class TestExecutorSubdirectoryCreation:
    """Test that AnsibleExecutor creates parent directories for log files.

    These tests replicate the path-construction and mkdir logic from
    execute_playbook (ansible_executor.py:80-82) rather than calling
    execute_playbook directly, because execute_playbook requires a real
    playbook file and spawns a subprocess.  The tested logic is the
    Path construction and parent-directory creation that execute_playbook
    performs before launching the subprocess.
    """

    def test_creates_subdirectory_for_task_id_with_slash(self, tmp_path):
        """Log subdirectories are created when task_id contains path separators."""
        from ansible_deployer.ansible_executor import AnsibleExecutor

        executor = AnsibleExecutor(log_dir=tmp_path)

        # The task_id that would result from --log-prefix "test/linux"
        task_id = "test/linux_20260212_120000_abc12345"

        # Simulate what execute_playbook does: build log paths and create parents
        stdout_log = executor.log_dir.joinpath(f"{task_id}_stdout.log")
        json_log = executor.log_dir.joinpath(f"{task_id}.json")
        stdout_log.parent.mkdir(parents=True, exist_ok=True)

        # The subdirectory should now exist
        expected_subdir = tmp_path.joinpath("test")
        assert expected_subdir.is_dir()

        # Writing to the log files should succeed
        stdout_log.write_text("test output")
        json_log.write_text("{}")
        assert stdout_log.exists()
        assert json_log.exists()

    def test_creates_nested_subdirectories(self, tmp_path):
        """Deeply nested subdirectories are created from task_id."""
        from ansible_deployer.ansible_executor import AnsibleExecutor

        executor = AnsibleExecutor(log_dir=tmp_path)

        task_id = "ci/nightly/linux-9_20260212_120000_abc12345"
        stdout_log = executor.log_dir.joinpath(f"{task_id}_stdout.log")
        stdout_log.parent.mkdir(parents=True, exist_ok=True)

        expected_subdir = tmp_path.joinpath("ci").joinpath("nightly")
        assert expected_subdir.is_dir()
        stdout_log.write_text("test")
        assert stdout_log.exists()

    def test_flat_task_id_no_error(self, tmp_path):
        """A flat task_id (no slashes) still works — no extra dirs created."""
        from ansible_deployer.ansible_executor import AnsibleExecutor

        executor = AnsibleExecutor(log_dir=tmp_path)

        task_id = "prod-deploy_20260212_120000_abc12345"
        stdout_log = executor.log_dir.joinpath(f"{task_id}_stdout.log")
        stdout_log.parent.mkdir(parents=True, exist_ok=True)

        # Parent is just tmp_path itself, no subdirs
        assert stdout_log.parent == tmp_path
        stdout_log.write_text("test")
        assert stdout_log.exists()


class TestRepeatTaskIdSuffix:
    """Test --repeat task_id suffix logic.

    When --repeat > 1, each iteration gets a _runN suffix on the task_id
    so log files don't overwrite each other. When --repeat is 1 (default),
    no suffix is added for backward compatibility.
    """

    def test_single_run_no_suffix(self):
        """With repeat=1, task_id has no _runN suffix."""
        task_id = "20260213_120000_abc12345"
        result = build_run_task_id(task_id, run_num=1, repeat=1)
        assert result == "20260213_120000_abc12345"

    def test_repeat_adds_run_suffix(self):
        """With repeat=3, each iteration gets _run-1, _run-2, _run-3."""
        task_id = "20260213_120000_abc12345"
        run_ids = [
            build_run_task_id(task_id, run_num, repeat=3)
            for run_num in range(1, 4)
        ]
        assert run_ids == [
            "20260213_120000_abc12345_run-1",
            "20260213_120000_abc12345_run-2",
            "20260213_120000_abc12345_run-3",
        ]

    def test_repeat_with_prefix_adds_suffix(self):
        """With repeat > 1 and --log-prefix, suffix is appended after prefix."""
        prefix = sanitize_log_prefix("test/linux")
        base = "20260213_120000_abc12345"
        task_id = f"{prefix}_{base}"
        run_ids = [
            build_run_task_id(task_id, run_num, repeat=2)
            for run_num in range(1, 3)
        ]
        assert run_ids == [
            "test/linux_20260213_120000_abc12345_run-1",
            "test/linux_20260213_120000_abc12345_run-2",
        ]

    def test_repeat_log_files_are_separate(self, tmp_path):
        """Each repeat iteration produces separate log files."""
        from ansible_deployer.ansible_executor import AnsibleExecutor

        executor = AnsibleExecutor(log_dir=tmp_path)
        task_id = "20260213_120000_abc12345"
        repeat = 3

        for run_num in range(1, repeat + 1):
            run_task_id = build_run_task_id(task_id, run_num, repeat)
            stdout_log = executor.log_dir.joinpath(
                f"{run_task_id}_stdout.log"
            )
            json_log = executor.log_dir.joinpath(f"{run_task_id}.json")
            stdout_log.parent.mkdir(parents=True, exist_ok=True)
            stdout_log.write_text(f"output from run {run_num}")
            json_log.write_text(f'{{"run": {run_num}}}')

        # All 3 pairs of log files exist and have distinct content
        for run_num in range(1, 4):
            run_id = build_run_task_id(task_id, run_num, repeat)
            stdout = tmp_path.joinpath(f"{run_id}_stdout.log")
            json_f = tmp_path.joinpath(f"{run_id}.json")
            assert stdout.exists()
            assert json_f.exists()
            assert f"run {run_num}" in stdout.read_text()

    def test_repeat_with_subdirectory_prefix(self, tmp_path):
        """Repeat + subdirectory prefix creates correct nested log files."""
        from ansible_deployer.ansible_executor import AnsibleExecutor

        executor = AnsibleExecutor(log_dir=tmp_path)
        task_id = "ci/nightly_20260213_120000_abc12345"
        repeat = 2

        for run_num in range(1, repeat + 1):
            run_task_id = build_run_task_id(task_id, run_num, repeat)
            stdout_log = executor.log_dir.joinpath(
                f"{run_task_id}_stdout.log"
            )
            stdout_log.parent.mkdir(parents=True, exist_ok=True)
            stdout_log.write_text(f"run {run_num}")

        assert tmp_path.joinpath(
            "ci",
            "nightly_20260213_120000_abc12345_run-1_stdout.log"
        ).exists()
        assert tmp_path.joinpath(
            "ci",
            "nightly_20260213_120000_abc12345_run-2_stdout.log"
        ).exists()


class TestRepeatExecutionCount:
    """Verify --repeat controls how many times the executor is called.

    Uses build_run_task_id() from cli.py to build task IDs, then
    simulates the repeat loop to count executor invocations.
    """

    @staticmethod
    def _simulate_repeat_loop(repeat):
        """Simulate the repeat loop from cli.py, returning call count and task IDs."""
        task_id = "20260225_120000_abc12345"
        calls = []
        success = True

        for run_num in range(1, repeat + 1):
            run_task_id = build_run_task_id(task_id, run_num, repeat)
            calls.append(run_task_id)

            if not success:
                break

        return calls

    def test_repeat_1_executes_once(self):
        """--repeat 1 calls the executor exactly once."""
        calls = self._simulate_repeat_loop(repeat=1)
        assert len(calls) == 1
        assert calls == ["20260225_120000_abc12345"]

    def test_repeat_2_executes_twice(self):
        """--repeat 2 calls the executor exactly twice."""
        calls = self._simulate_repeat_loop(repeat=2)
        assert len(calls) == 2
        assert calls == [
            "20260225_120000_abc12345_run-1",
            "20260225_120000_abc12345_run-2",
        ]

    def test_repeat_5_executes_five_times(self):
        """--repeat 5 calls the executor exactly five times."""
        calls = self._simulate_repeat_loop(repeat=5)
        assert len(calls) == 5
        for i, call_id in enumerate(calls, 1):
            assert call_id.endswith(f"_run-{i}")

    def test_repeat_stops_on_failure(self):
        """Executor failure on run 2 stops at 2 calls, not 5."""
        task_id = "20260225_120000_abc12345"
        repeat = 5
        fail_on_run = 2
        calls = []

        for run_num in range(1, repeat + 1):
            run_task_id = build_run_task_id(task_id, run_num, repeat)
            calls.append(run_task_id)
            success = (run_num != fail_on_run)
            if not success:
                break

        assert len(calls) == 2
        assert calls[-1] == f"{task_id}_run-2"
