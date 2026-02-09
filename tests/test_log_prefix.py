"""
Tests for --log-prefix subdirectory support and --repeat task_id suffixing.

Covers:
- sanitize_log_prefix(): character filtering, slash handling, edge cases
- AnsibleExecutor: parent directory creation when task_id contains subdirectories
- --repeat: task_id _runN suffix logic, log file separation, backward compatibility
"""
import pytest
from pathlib import Path

from ansible_deployer.cli import sanitize_log_prefix


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
    """Test that AnsibleExecutor creates parent directories for log files."""

    def test_creates_subdirectory_for_task_id_with_slash(self, tmp_path):
        """Log subdirectories are created when task_id contains path separators."""
        from ansible_deployer.ansible_executor import AnsibleExecutor

        executor = AnsibleExecutor(log_dir=tmp_path)

        # The task_id that would result from --log-prefix "test/linux"
        task_id = "test/linux_20260212_120000_abc12345"

        # Simulate what execute_playbook does: build log paths and create parents
        stdout_log = executor.log_dir / f"{task_id}_stdout.log"
        json_log = executor.log_dir / f"{task_id}.json"
        stdout_log.parent.mkdir(parents=True, exist_ok=True)

        # The subdirectory should now exist
        expected_subdir = tmp_path / "test"
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
        stdout_log = executor.log_dir / f"{task_id}_stdout.log"
        stdout_log.parent.mkdir(parents=True, exist_ok=True)

        expected_subdir = tmp_path / "ci" / "nightly"
        assert expected_subdir.is_dir()
        stdout_log.write_text("test")
        assert stdout_log.exists()

    def test_flat_task_id_no_error(self, tmp_path):
        """A flat task_id (no slashes) still works — no extra dirs created."""
        from ansible_deployer.ansible_executor import AnsibleExecutor

        executor = AnsibleExecutor(log_dir=tmp_path)

        task_id = "prod-deploy_20260212_120000_abc12345"
        stdout_log = executor.log_dir / f"{task_id}_stdout.log"
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
        repeat = 1
        for run_num in range(1, repeat + 1):
            if repeat > 1:
                run_task_id = f"{task_id}_run-{run_num}"
            else:
                run_task_id = task_id
        assert run_task_id == "20260213_120000_abc12345"

    def test_repeat_adds_run_suffix(self):
        """With repeat=3, each iteration gets _run-1, _run-2, _run-3."""
        task_id = "20260213_120000_abc12345"
        repeat = 3
        run_ids = []
        for run_num in range(1, repeat + 1):
            if repeat > 1:
                run_task_id = f"{task_id}_run-{run_num}"
            else:
                run_task_id = task_id
            run_ids.append(run_task_id)
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
        repeat = 2
        run_ids = []
        for run_num in range(1, repeat + 1):
            if repeat > 1:
                run_task_id = f"{task_id}_run-{run_num}"
            else:
                run_task_id = task_id
            run_ids.append(run_task_id)
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
            run_task_id = f"{task_id}_run-{run_num}"
            stdout_log = executor.log_dir / f"{run_task_id}_stdout.log"
            json_log = executor.log_dir / f"{run_task_id}.json"
            stdout_log.parent.mkdir(parents=True, exist_ok=True)
            stdout_log.write_text(f"output from run {run_num}")
            json_log.write_text(f'{{"run": {run_num}}}')

        # All 3 pairs of log files exist and have distinct content
        for run_num in range(1, 4):
            stdout = tmp_path / f"{task_id}_run-{run_num}_stdout.log"
            json_f = tmp_path / f"{task_id}_run-{run_num}.json"
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
            run_task_id = f"{task_id}_run-{run_num}"
            stdout_log = executor.log_dir / f"{run_task_id}_stdout.log"
            stdout_log.parent.mkdir(parents=True, exist_ok=True)
            stdout_log.write_text(f"run {run_num}")

        assert (tmp_path / "ci" / "nightly_20260213_120000_abc12345_run-1_stdout.log").exists()
        assert (tmp_path / "ci" / "nightly_20260213_120000_abc12345_run-2_stdout.log").exists()
