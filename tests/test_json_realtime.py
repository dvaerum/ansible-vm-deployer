"""
Tests for the json_realtime Ansible callback plugin.

Since ansible-core is not available as a Python library in the test
environment (only as a CLI tool via Nix), we mock the Ansible module
imports before loading the callback plugin.

Covers:
- UTC timestamp format
- Play and task data structures (including path field)
- Task result recording (filtering, status fields, duration)
- Loop data preservation (record-before-super bug fix)
- Handler task recording
- Include event recording
- Retry event recording
- Stats format (matches official ansible.posix.json callback)
- JSON file output
"""

import datetime
import json
import os
import sys
from unittest.mock import Mock, MagicMock, patch

import pytest


# ── Mock Ansible Framework ────────────────────────────────────────
#
# Ansible is not importable in the test environment, so we install
# lightweight stand-ins for the callback base classes before
# importing the plugin under test. These mocks provide just enough
# structure for the plugin's __init__ and super() calls to work.


class _MockCallbackBase:
    """Stand-in for ansible.plugins.callback.CallbackBase."""

    def __init__(self, display=None, options=None):
        self._display = display or Mock()

    def _get_item_label(self, result_vars):
        """Return item label for loop items (returns None = no label)."""
        return None


class _MockDefaultCallback(_MockCallbackBase):
    """Stand-in for ansible.plugins.callback.default.CallbackModule."""

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stdout'
    CALLBACK_NAME = 'default'

    def __init__(self):
        self._play = None
        self._last_task_banner = None
        self._last_task_name = None
        self._task_type_cache = {}
        super().__init__()

    def v2_playbook_on_play_start(self, play):
        pass

    def v2_playbook_on_task_start(self, task, is_conditional):
        pass

    def v2_playbook_on_handler_task_start(self, task):
        pass

    def v2_playbook_on_include(self, included_file):
        pass

    def v2_runner_on_ok(self, result):
        pass

    def v2_runner_on_failed(self, result, ignore_errors=False):
        pass

    def v2_runner_on_skipped(self, result):
        pass

    def v2_runner_on_unreachable(self, result):
        pass

    def v2_runner_on_start(self, host, task):
        pass

    def v2_runner_item_on_skipped(self, result):
        pass

    def v2_runner_retry(self, result):
        pass

    def v2_playbook_on_stats(self, stats):
        pass


# Install mocks into sys.modules before importing the plugin
_mock_default_module = type(sys)('mock_ansible_default_callback')
_mock_default_module.CallbackModule = _MockDefaultCallback

_mock_modules = {
    'ansible': MagicMock(),
    'ansible.plugins': MagicMock(),
    'ansible.plugins.callback': MagicMock(),
    'ansible.plugins.callback.default': _mock_default_module,
}
for _name, _mod in _mock_modules.items():
    sys.modules[_name] = _mod

# Now safe to import the callback plugin
from ansible_deployer.ansible_callbacks.json_realtime import (
    CallbackModule,
    _current_time,
)


# ── Mock Helpers ──────────────────────────────────────────────────


def _mock_play(
    name='test play',
    uuid='play-uuid-1234',
    path='/playbooks/site.yml:1',
):
    """Create a mock Ansible Play object."""
    play = Mock()
    play.get_name.return_value = name
    play._uuid = uuid
    play.get_path.return_value = path
    play.strategy = 'linear'
    play.check_mode = False
    return play


def _mock_task(
    name='test task',
    uuid='task-uuid-5678',
    path='/playbooks/site.yml:5',
    action='debug',
):
    """Create a mock Ansible Task object."""
    task = Mock()
    task.get_name.return_value = name
    task._uuid = uuid
    task.get_path.return_value = path
    task.action = action
    task.no_log = False
    task.loop = None
    task.check_mode = False
    task.args = {}
    return task


def _mock_result(
    host_name='testhost',
    task=None,
    result_data=None,
):
    """Create a mock Ansible TaskResult object."""
    result = Mock()
    result._host = Mock()
    result._host.get_name.return_value = host_name
    result._host.name = host_name
    result._task = task or _mock_task()
    result._result = result_data if result_data is not None else {
        'changed': False,
    }
    return result


def _mock_stats(host_summaries=None, custom=None):
    """Create a mock Ansible Stats object."""
    stats = Mock()
    host_summaries = host_summaries or {
        'testhost': {
            'ok': 5, 'changed': 2, 'unreachable': 0,
            'failures': 0, 'skipped': 1, 'rescued': 0,
            'ignored': 0,
        },
    }
    stats.processed = {
        h: {} for h in host_summaries.keys()
    }
    stats.summarize = lambda h: host_summaries[h]
    stats.custom = custom
    return stats


@pytest.fixture
def callback(tmp_path):
    """Create a CallbackModule instance for testing."""
    cb = CallbackModule()
    cb.json_log_path = str(tmp_path.joinpath('output.json'))
    return cb


# ── TestCurrentTime ───────────────────────────────────────────────


class TestCurrentTime:
    """Tests for the _current_time() helper."""

    def test_returns_utc_iso_format_with_z_suffix(self):
        """Timestamp matches official ansible.posix.json format:
        ISO 8601 UTC with Z suffix."""
        ts = _current_time()
        assert ts.endswith('Z')
        # Should be parseable as ISO datetime (strip the Z)
        parsed = datetime.datetime.fromisoformat(ts[:-1])
        assert isinstance(parsed, datetime.datetime)


# ── TestNewPlay ───────────────────────────────────────────────────


class TestNewPlay:
    """Tests for _new_play() helper."""

    def test_has_required_fields(self, callback):
        """Play structure has name, id, path, duration.start."""
        play = _mock_play(
            name='Deploy App', uuid='abc-123',
            path='/site.yml:1',
        )
        data = callback._new_play(play)

        assert data['play']['name'] == 'Deploy App'
        assert data['play']['id'] == 'abc-123'
        assert data['play']['duration']['start'].endswith('Z')
        assert data['tasks'] == []

    def test_includes_path(self, callback):
        """Play structure includes path field matching official
        ansible.posix.json format."""
        play = _mock_play(path='/playbooks/deploy.yml:3')
        data = callback._new_play(play)

        assert data['play']['path'] == '/playbooks/deploy.yml:3'


# ── TestNewTask ───────────────────────────────────────────────────


class TestNewTask:
    """Tests for _new_task() helper."""

    def test_has_required_fields(self, callback):
        """Task structure has name, id, path, duration.start."""
        task = _mock_task(
            name='Install packages', uuid='def-456',
            path='/roles/base/tasks/main.yml:10',
        )
        data = callback._new_task(task)

        assert data['task']['name'] == 'Install packages'
        assert data['task']['id'] == 'def-456'
        assert data['task']['duration']['start'].endswith('Z')
        assert data['hosts'] == {}

    def test_includes_path(self, callback):
        """Task structure includes path field matching official
        ansible.posix.json format."""
        task = _mock_task(
            path='/roles/base/tasks/main.yml:10',
        )
        data = callback._new_task(task)

        assert data['task']['path'] == (
            '/roles/base/tasks/main.yml:10'
        )


# ── TestRecordTaskResult ──────────────────────────────────────────


class TestRecordTaskResult:
    """Tests for _record_task_result() — the core JSON recording
    method."""

    def test_records_host_result(self, callback):
        """Result is stored under hosts[hostname]."""
        callback._current_play = callback._new_play(_mock_play())
        callback._current_task = callback._new_task(_mock_task())

        result = _mock_result(
            host_name='web-1',
            result_data={'changed': True, 'msg': 'ok'},
        )
        callback._record_task_result(result, 'ok')

        assert 'web-1' in callback._current_task['hosts']
        host_data = callback._current_task['hosts']['web-1']
        assert host_data['changed'] is True
        assert host_data['msg'] == 'ok'

    def test_filters_internal_ansible_keys(self, callback):
        """Keys starting with _ansible are excluded from JSON."""
        callback._current_play = callback._new_play(_mock_play())
        callback._current_task = callback._new_task(_mock_task())

        result = _mock_result(result_data={
            'changed': False,
            'msg': 'ok',
            '_ansible_verbose_always': True,
            '_ansible_no_log': False,
            '_ansible_parsed': True,
        })
        callback._record_task_result(result, 'ok')

        host_data = callback._current_task['hosts']['testhost']
        assert 'msg' in host_data
        assert '_ansible_verbose_always' not in host_data
        assert '_ansible_no_log' not in host_data
        assert '_ansible_parsed' not in host_data

    def test_preserves_loop_results_data(self, callback):
        """Loop item data in result['results'] is preserved.

        This is the critical bug fix: the parent's _process_items()
        deletes result._result['results'] for loop tasks. Since we
        record BEFORE calling super(), the loop data is captured.
        """
        callback._current_play = callback._new_play(_mock_play())
        callback._current_task = callback._new_task(_mock_task())

        item_results = [
            {'item': 'pkg1', 'changed': True},
            {'item': 'pkg2', 'changed': False},
            {'item': 'pkg3', 'changed': True},
        ]
        result = _mock_result(result_data={
            'changed': True,
            'results': item_results,
        })

        # Record first (this is what our plugin does)
        callback._record_task_result(result, 'ok')

        # Simulate what _process_items does after super()
        del result._result['results']

        # The JSON data should still have the loop results
        host_data = callback._current_task['hosts']['testhost']
        assert 'results' in host_data
        assert len(host_data['results']) == 3
        assert host_data['results'][0]['item'] == 'pkg1'

    def test_sets_ok_status(self, callback):
        """Status 'ok' sets failed/skipped/unreachable to False."""
        callback._current_play = callback._new_play(_mock_play())
        callback._current_task = callback._new_task(_mock_task())

        result = _mock_result()
        callback._record_task_result(result, 'ok')

        host_data = callback._current_task['hosts']['testhost']
        assert host_data['failed'] is False
        assert host_data['skipped'] is False
        assert host_data['unreachable'] is False

    def test_sets_failed_status(self, callback):
        """Status 'failed' sets failed=True."""
        callback._current_play = callback._new_play(_mock_play())
        callback._current_task = callback._new_task(_mock_task())

        result = _mock_result(result_data={
            'changed': False,
            'msg': 'module failed',
        })
        callback._record_task_result(result, 'failed')

        host_data = callback._current_task['hosts']['testhost']
        assert host_data['failed'] is True

    def test_sets_skipped_status(self, callback):
        """Status 'skipped' sets skipped=True."""
        callback._current_play = callback._new_play(_mock_play())
        callback._current_task = callback._new_task(_mock_task())

        result = _mock_result()
        callback._record_task_result(result, 'skipped')

        host_data = callback._current_task['hosts']['testhost']
        assert host_data['skipped'] is True

    def test_sets_unreachable_status(self, callback):
        """Status 'unreachable' sets unreachable=True."""
        callback._current_play = callback._new_play(_mock_play())
        callback._current_task = callback._new_task(_mock_task())

        result = _mock_result()
        callback._record_task_result(result, 'unreachable')

        host_data = callback._current_task['hosts']['testhost']
        assert host_data['unreachable'] is True

    def test_adds_action(self, callback):
        """Task action is recorded in the result."""
        callback._current_play = callback._new_play(_mock_play())
        task = _mock_task(action='yum')
        callback._current_task = callback._new_task(task)

        result = _mock_result(task=task)
        callback._record_task_result(result, 'ok')

        host_data = callback._current_task['hosts']['testhost']
        assert host_data['action'] == 'yum'

    def test_updates_duration(self, callback):
        """Recording a result sets duration.end on task and play."""
        callback._current_play = callback._new_play(_mock_play())
        callback._current_task = callback._new_task(_mock_task())

        result = _mock_result()
        callback._record_task_result(result, 'ok')

        task_end = callback._current_task['task']['duration']['end']
        play_end = callback._current_play['play']['duration']['end']
        assert task_end.endswith('Z')
        assert play_end.endswith('Z')

    def test_noop_when_no_current_task(self, callback):
        """Does nothing when no task is active (no crash, no
        side effects)."""
        callback._current_task = None
        results_before = list(callback.results)
        play_before = callback._current_play
        result = _mock_result()
        # Should not raise
        callback._record_task_result(result, 'ok')
        # Should not modify results or _current_play
        assert callback.results == results_before
        assert callback._current_play is play_before


# ── TestPlayLifecycle ─────────────────────────────────────────────


class TestPlayLifecycle:
    """Tests for play start recording."""

    def test_play_start_appends_to_results(self, callback):
        """v2_playbook_on_play_start appends play to results."""
        play = _mock_play(name='Deploy')
        callback.v2_playbook_on_play_start(play)

        assert len(callback.results) == 1
        assert callback.results[0]['play']['name'] == 'Deploy'

    def test_multiple_plays_appended(self, callback):
        """Multiple plays are all recorded."""
        callback.v2_playbook_on_play_start(
            _mock_play(name='Play 1', uuid='p1')
        )
        callback.v2_playbook_on_play_start(
            _mock_play(name='Play 2', uuid='p2')
        )
        callback.v2_playbook_on_play_start(
            _mock_play(name='Play 3', uuid='p3')
        )

        assert len(callback.results) == 3
        names = [p['play']['name'] for p in callback.results]
        assert names == ['Play 1', 'Play 2', 'Play 3']


# ── TestTaskLifecycle ─────────────────────────────────────────────


class TestTaskLifecycle:
    """Tests for task recording (regular, handler, include)."""

    def test_task_start_appends_to_current_play(self, callback):
        """v2_playbook_on_task_start appends task to current play."""
        callback.v2_playbook_on_play_start(_mock_play())
        callback.v2_playbook_on_task_start(
            _mock_task(name='Install'), is_conditional=False
        )

        tasks = callback.results[0]['tasks']
        assert len(tasks) == 1
        assert tasks[0]['task']['name'] == 'Install'

    def test_handler_task_appended(self, callback):
        """Handler tasks are recorded like regular tasks."""
        callback.v2_playbook_on_play_start(_mock_play())
        callback.v2_playbook_on_task_start(
            _mock_task(name='Install nginx'), is_conditional=False
        )
        callback.v2_playbook_on_handler_task_start(
            _mock_task(name='Restart nginx', uuid='handler-1')
        )

        tasks = callback.results[0]['tasks']
        assert len(tasks) == 2
        assert tasks[1]['task']['name'] == 'Restart nginx'

    def test_include_event_recorded(self, callback):
        """Include events are recorded in the tasks list."""
        callback.v2_playbook_on_play_start(_mock_play())

        included_file = Mock()
        included_file._filename = '/roles/web/tasks/install.yml'
        host1 = Mock()
        host1.name = 'web-1'
        host2 = Mock()
        host2.name = 'web-2'
        included_file._hosts = [host1, host2]
        included_file._vars = {}

        callback.v2_playbook_on_include(included_file)

        tasks = callback.results[0]['tasks']
        assert len(tasks) == 1
        include = tasks[0]['include']
        assert include['file'] == '/roles/web/tasks/install.yml'
        assert include['hosts'] == ['web-1', 'web-2']

    def test_include_with_item_label(self, callback):
        """Include events record item label for loop includes."""
        callback.v2_playbook_on_play_start(_mock_play())

        # Override _get_item_label to return a label
        callback._get_item_label = lambda vars: 'redhat'

        included_file = Mock()
        included_file._filename = '/tasks/distro.yml'
        host = Mock()
        host.name = 'host-1'
        included_file._hosts = [host]
        included_file._vars = {'distro': 'redhat'}

        callback.v2_playbook_on_include(included_file)

        include = callback.results[0]['tasks'][0]['include']
        assert include['item'] == 'redhat'


# ── TestRunnerResults ─────────────────────────────────────────────


class TestRunnerResults:
    """Tests for runner result methods."""

    def test_ok_records_to_json(self, callback):
        """v2_runner_on_ok records result in JSON."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task(name='Copy file')
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        result = _mock_result(
            host_name='app-1', task=task,
            result_data={'changed': True, 'dest': '/etc/foo'},
        )
        callback.v2_runner_on_ok(result)

        hosts = callback.results[0]['tasks'][0]['hosts']
        assert 'app-1' in hosts
        assert hosts['app-1']['changed'] is True
        assert hosts['app-1']['dest'] == '/etc/foo'
        assert hosts['app-1']['failed'] is False

    def test_failed_records_to_json(self, callback):
        """v2_runner_on_failed records result with failed=True."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task(name='Start service')
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        result = _mock_result(
            host_name='app-1', task=task,
            result_data={
                'changed': False,
                'msg': 'service not found',
            },
        )
        callback.v2_runner_on_failed(result, ignore_errors=False)

        hosts = callback.results[0]['tasks'][0]['hosts']
        assert hosts['app-1']['failed'] is True
        assert hosts['app-1']['msg'] == 'service not found'

    def test_skipped_records_to_json(self, callback):
        """v2_runner_on_skipped records result with skipped=True."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task()
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        result = _mock_result(task=task)
        callback.v2_runner_on_skipped(result)

        hosts = callback.results[0]['tasks'][0]['hosts']
        assert hosts['testhost']['skipped'] is True

    def test_unreachable_records_to_json(self, callback):
        """v2_runner_on_unreachable records with unreachable=True."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task()
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        result = _mock_result(
            task=task,
            result_data={
                'changed': False, 'unreachable': True,
                'msg': 'SSH connection failed',
            },
        )
        callback.v2_runner_on_unreachable(result)

        hosts = callback.results[0]['tasks'][0]['hosts']
        assert hosts['testhost']['unreachable'] is True

    def test_multiple_hosts_per_task(self, callback):
        """Multiple hosts can have results for the same task."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task()
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        for host_name in ['web-1', 'web-2', 'web-3']:
            result = _mock_result(
                host_name=host_name, task=task,
                result_data={'changed': True},
            )
            callback.v2_runner_on_ok(result)

        hosts = callback.results[0]['tasks'][0]['hosts']
        assert len(hosts) == 3
        assert all(
            hosts[h]['changed'] is True
            for h in ['web-1', 'web-2', 'web-3']
        )

    def test_failed_with_ignore_errors_recorded(self, callback):
        """v2_runner_on_failed with ignore_errors=True records the
        flag in JSON so consumers can distinguish ignored failures."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task(name='Allowed to fail')
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        result = _mock_result(
            host_name='app-1', task=task,
            result_data={
                'changed': False,
                'msg': 'expected failure',
            },
        )
        callback.v2_runner_on_failed(result, ignore_errors=True)

        hosts = callback.results[0]['tasks'][0]['hosts']
        assert hosts['app-1']['failed'] is True
        assert hosts['app-1']['ignore_errors'] is True

    def test_failed_without_ignore_errors_omits_key(self, callback):
        """When ignore_errors is False (default), the key is omitted
        from JSON to keep output clean."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task(name='Must succeed')
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        result = _mock_result(
            host_name='app-1', task=task,
            result_data={'changed': False, 'msg': 'fatal'},
        )
        callback.v2_runner_on_failed(result, ignore_errors=False)

        hosts = callback.results[0]['tasks'][0]['hosts']
        assert hosts['app-1']['failed'] is True
        assert 'ignore_errors' not in hosts['app-1']


# ── TestLoopDataPreservation ──────────────────────────────────────


class TestLoopDataPreservation:
    """Verify the record-before-super fix for loop tasks.

    The parent callback's _process_items() deletes
    result._result['results'] after displaying individual items.
    Our plugin must record BEFORE super() to preserve this data.
    """

    def test_loop_data_survives_process_items(self, callback):
        """Loop item results are preserved in JSON even after
        _process_items deletes them from the result object."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task(name='Install packages', action='yum')
        task.loop = 'items'
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        items = [
            {
                'item': 'httpd', 'changed': True,
                'msg': 'installed',
            },
            {
                'item': 'nginx', 'changed': False,
                'msg': 'already installed',
            },
        ]
        result = _mock_result(
            task=task,
            result_data={
                'changed': True,
                'results': items,
                'msg': 'All items completed',
            },
        )

        # This is the exact sequence in v2_runner_on_ok:
        # 1. Record to JSON (captures results)
        callback._record_task_result(result, 'ok')
        # 2. Parent's _process_items deletes results
        del result._result['results']

        # Verify JSON still has loop data
        host_data = (
            callback.results[0]['tasks'][0]['hosts']['testhost']
        )
        assert 'results' in host_data
        assert len(host_data['results']) == 2
        assert host_data['results'][0]['item'] == 'httpd'
        assert host_data['results'][1]['item'] == 'nginx'

    def test_diff_data_preserved(self, callback):
        """Diff data from --diff mode is preserved in JSON."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task(action='copy')
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        result = _mock_result(
            task=task,
            result_data={
                'changed': True,
                'diff': {
                    'before': 'old content\n',
                    'after': 'new content\n',
                },
            },
        )
        callback._record_task_result(result, 'ok')

        host_data = (
            callback.results[0]['tasks'][0]['hosts']['testhost']
        )
        assert 'diff' in host_data
        assert host_data['diff']['before'] == 'old content\n'
        assert host_data['diff']['after'] == 'new content\n'


# ── TestRetry ─────────────────────────────────────────────────────


class TestRetry:
    """Tests for retry event recording."""

    def test_retry_recorded_per_host(self, callback):
        """v2_runner_retry records attempt info per host."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task(name='Wait for service')
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        result = _mock_result(
            host_name='app-1', task=task,
            result_data={'attempts': 1, 'retries': 3},
        )
        callback.v2_runner_retry(result)

        retries = callback._current_task['retries']
        assert 'app-1' in retries
        assert len(retries['app-1']) == 1
        assert retries['app-1'][0]['attempts'] == 1
        assert retries['app-1'][0]['retries'] == 3

    def test_multiple_retries_accumulated(self, callback):
        """Multiple retry events for the same host accumulate."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task()
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        for attempt in range(1, 4):
            result = _mock_result(
                host_name='app-1', task=task,
                result_data={'attempts': attempt, 'retries': 5},
            )
            callback.v2_runner_retry(result)

        retries = callback._current_task['retries']['app-1']
        assert len(retries) == 3
        assert [r['attempts'] for r in retries] == [1, 2, 3]


# ── TestStats ─────────────────────────────────────────────────────


class TestStats:
    """Tests for stats recording and JSON output."""

    def test_stats_uses_summarize_directly(self, callback, tmp_path):
        """Stats format matches official ansible.posix.json callback:
        uses stats.summarize() as-is (key is 'failures', not 'failed').
        """
        callback.v2_playbook_on_play_start(_mock_play())

        stats = _mock_stats({
            'web-1': {
                'ok': 10, 'changed': 3, 'unreachable': 0,
                'failures': 1, 'skipped': 2, 'rescued': 0,
                'ignored': 0,
            },
        })
        callback.v2_playbook_on_stats(stats)

        with open(callback.json_log_path) as f:
            output = json.load(f)

        assert 'web-1' in output['stats']
        host_stats = output['stats']['web-1']
        # Must use 'failures' (not 'failed') to match official format
        assert host_stats['failures'] == 1
        assert 'failed' not in host_stats
        assert host_stats['ok'] == 10
        assert host_stats['changed'] == 3

    def test_custom_stats_separated(self, callback, tmp_path):
        """Per-host custom stats are in 'custom_stats', global
        custom stats in 'global_custom_stats'."""
        callback.v2_playbook_on_play_start(_mock_play())

        custom = {
            'web-1': {'deploy_time': 42},
            '_run': {'total_deploys': 5},
        }
        stats = _mock_stats(
            host_summaries={
                'web-1': {
                    'ok': 5, 'changed': 0, 'unreachable': 0,
                    'failures': 0, 'skipped': 0, 'rescued': 0,
                    'ignored': 0,
                },
            },
            custom=custom,
        )
        callback.v2_playbook_on_stats(stats)

        with open(callback.json_log_path) as f:
            output = json.load(f)

        assert output['global_custom_stats'] == {
            'total_deploys': 5,
        }
        assert output['custom_stats'] == {
            'web-1': {'deploy_time': 42},
        }

    def test_json_written_to_file(self, callback, tmp_path):
        """JSON output is written to the configured path."""
        callback.v2_playbook_on_play_start(
            _mock_play(name='My Play')
        )
        task = _mock_task(name='My Task')
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )
        result = _mock_result(
            task=task,
            result_data={'changed': True, 'msg': 'done'},
        )
        callback.v2_runner_on_ok(result)
        callback.v2_playbook_on_stats(_mock_stats())

        assert os.path.exists(callback.json_log_path)

        with open(callback.json_log_path) as f:
            output = json.load(f)

        assert len(output['plays']) == 1
        assert output['plays'][0]['play']['name'] == 'My Play'
        tasks = output['plays'][0]['tasks']
        assert len(tasks) == 1
        assert tasks[0]['task']['name'] == 'My Task'
        assert 'testhost' in tasks[0]['hosts']

    def test_json_has_path_fields(self, callback, tmp_path):
        """JSON output includes path fields for plays and tasks."""
        callback.v2_playbook_on_play_start(
            _mock_play(path='/playbook.yml:1')
        )
        task = _mock_task(path='/roles/web/tasks/main.yml:5')
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )
        result = _mock_result(task=task)
        callback.v2_runner_on_ok(result)
        callback.v2_playbook_on_stats(_mock_stats())

        with open(callback.json_log_path) as f:
            output = json.load(f)

        assert output['plays'][0]['play']['path'] == (
            '/playbook.yml:1'
        )
        assert output['plays'][0]['tasks'][0]['task']['path'] == (
            '/roles/web/tasks/main.yml:5'
        )


# ── TestWriteFailure ──────────────────────────────────────────────


class TestWriteFailure:
    """Tests for the JSON write failure path in v2_playbook_on_stats."""

    def test_write_failure_shows_warning(self, callback):
        """When JSON file write fails, a warning is displayed
        instead of crashing the playbook."""
        # Point to an unwritable path
        callback.json_log_path = '/nonexistent/dir/output.json'

        callback.v2_playbook_on_play_start(_mock_play())
        stats = _mock_stats()
        callback.v2_playbook_on_stats(stats)

        # Should have called _display.warning with the error
        callback._display.warning.assert_called_once()
        warning_msg = callback._display.warning.call_args[0][0]
        assert '/nonexistent/dir/output.json' in warning_msg


# ── TestForwardingMethods ─────────────────────────────────────────


class TestForwardingMethods:
    """Tests for methods that forward to the parent without
    recording JSON data (v2_runner_on_start, v2_runner_item_on_skipped).
    """

    def test_runner_on_start_does_not_crash(self, callback):
        """v2_runner_on_start forwards to parent without error."""
        host = Mock()
        task = _mock_task()
        # Should not raise
        callback.v2_runner_on_start(host, task)

    def test_runner_item_on_skipped_does_not_crash(self, callback):
        """v2_runner_item_on_skipped forwards to parent without
        error."""
        result = _mock_result()
        # Should not raise
        callback.v2_runner_item_on_skipped(result)


# ── TestRecordBeforeSuperOrdering ─────────────────────────────────


class TestRecordBeforeSuperOrdering:
    """Verify that v2_runner_on_ok/failed/skipped/unreachable call
    _record_task_result BEFORE super(), so that even if the parent
    destroys result._result['results'], the JSON still has loop data.
    """

    def _make_destructive_parent(self, method_name):
        """Return a function that deletes result._result['results']
        like the parent's _process_items does."""
        def destructive(self_cb, result, *args, **kwargs):
            result._result.pop('results', None)
        return destructive

    def _run_ordering_test(self, callback, method_name, **kwargs):
        """Set up a loop result, make the parent destructive,
        call the method, and assert JSON has loop data."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task(action='yum')
        task.loop = 'items'
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        items = [
            {'item': 'pkg1', 'changed': True},
            {'item': 'pkg2', 'changed': False},
        ]
        result = _mock_result(
            task=task,
            result_data={
                'changed': True,
                'results': list(items),
            },
        )

        original = getattr(_MockDefaultCallback, method_name)
        setattr(
            _MockDefaultCallback,
            method_name,
            self._make_destructive_parent(method_name),
        )
        try:
            getattr(callback, method_name)(result, **kwargs)
        finally:
            setattr(
                _MockDefaultCallback, method_name, original
            )

        host_data = (
            callback.results[0]['tasks'][0]['hosts']['testhost']
        )
        assert 'results' in host_data
        assert len(host_data['results']) == 2
        assert host_data['results'][0]['item'] == 'pkg1'

    def test_ok_records_before_super(self, callback):
        """v2_runner_on_ok records loop data before parent
        destroys it."""
        self._run_ordering_test(callback, 'v2_runner_on_ok')

    def test_failed_records_before_super(self, callback):
        """v2_runner_on_failed records loop data before parent
        destroys it."""
        self._run_ordering_test(
            callback, 'v2_runner_on_failed',
            ignore_errors=False,
        )

    def test_skipped_records_before_super(self, callback):
        """v2_runner_on_skipped records loop data before parent
        destroys it."""
        self._run_ordering_test(
            callback, 'v2_runner_on_skipped',
        )

    def test_unreachable_records_before_super(self, callback):
        """v2_runner_on_unreachable records loop data before
        parent destroys it."""
        self._run_ordering_test(
            callback, 'v2_runner_on_unreachable',
        )


# ── TestDeepCopyIndependence ─────────────────────────────────────


class TestDeepCopyIndependence:
    """Verify that recorded data is independent of later mutations
    to the original result object (deep copy, not reference)."""

    def test_recorded_data_independent_of_result_mutations(
        self, callback,
    ):
        """Mutating nested data after recording does not affect
        the JSON output."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task()
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        nested = {'key': 'original'}
        result = _mock_result(
            task=task,
            result_data={
                'changed': False,
                'nested': nested,
                'items': [1, 2, 3],
            },
        )
        callback._record_task_result(result, 'ok')

        # Mutate the original data after recording
        nested['key'] = 'mutated'
        result._result['items'].append(999)

        host_data = (
            callback.results[0]['tasks'][0]['hosts']['testhost']
        )
        assert host_data['nested']['key'] == 'original'
        assert host_data['items'] == [1, 2, 3]


# ── TestSuperCalls ───────────────────────────────────────────────


class TestSuperCalls:
    """Verify that each overridden method calls super() on the
    parent class."""

    def test_ok_calls_super(self, callback):
        """v2_runner_on_ok calls parent's v2_runner_on_ok."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task()
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )
        result = _mock_result(task=task)

        with patch.object(
            _MockDefaultCallback, 'v2_runner_on_ok',
        ) as spy:
            callback.v2_runner_on_ok(result)
            spy.assert_called_once_with(result)

    def test_failed_calls_super(self, callback):
        """v2_runner_on_failed calls parent's
        v2_runner_on_failed."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task()
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )
        result = _mock_result(task=task)

        with patch.object(
            _MockDefaultCallback, 'v2_runner_on_failed',
        ) as spy:
            callback.v2_runner_on_failed(
                result, ignore_errors=True,
            )
            spy.assert_called_once_with(result, True)

    def test_skipped_calls_super(self, callback):
        """v2_runner_on_skipped calls parent's
        v2_runner_on_skipped."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task()
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )
        result = _mock_result(task=task)

        with patch.object(
            _MockDefaultCallback, 'v2_runner_on_skipped',
        ) as spy:
            callback.v2_runner_on_skipped(result)
            spy.assert_called_once_with(result)

    def test_unreachable_calls_super(self, callback):
        """v2_runner_on_unreachable calls parent's
        v2_runner_on_unreachable."""
        callback.v2_playbook_on_play_start(_mock_play())
        task = _mock_task()
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )
        result = _mock_result(task=task)

        with patch.object(
            _MockDefaultCallback, 'v2_runner_on_unreachable',
        ) as spy:
            callback.v2_runner_on_unreachable(result)
            spy.assert_called_once_with(result)


# ── TestCurrentPlayNoneGuards ────────────────────────────────────


class TestCurrentPlayNoneGuards:
    """Tests for _current_play being None guards in production
    code (lines 208-209, 235)."""

    def test_task_start_without_play_sets_current_task(
        self, callback,
    ):
        """v2_playbook_on_task_start without a play still sets
        _current_task but does not crash."""
        callback._current_play = None
        task = _mock_task(name='Orphan task')
        callback.v2_playbook_on_task_start(
            task, is_conditional=False
        )

        assert callback._current_task is not None
        assert (
            callback._current_task['task']['name']
            == 'Orphan task'
        )
        # No tasks appended anywhere since there's no play
        assert callback.results == []

    def test_include_without_play_does_not_crash(
        self, callback,
    ):
        """v2_playbook_on_include without a play does not crash
        (the guard at line 235 prevents append)."""
        callback._current_play = None
        included_file = Mock()
        included_file._filename = '/tasks/inc.yml'
        host = Mock()
        host.name = 'h1'
        included_file._hosts = [host]
        included_file._vars = {}

        # Should not raise
        callback.v2_playbook_on_include(included_file)
        assert callback.results == []

    def test_record_task_result_without_play_updates_task_only(
        self, callback,
    ):
        """_record_task_result without _current_play still records
        the host result and task duration, just skips play
        duration update."""
        callback._current_play = None
        callback._current_task = callback._new_task(_mock_task())

        result = _mock_result(
            result_data={'changed': True, 'msg': 'done'},
        )
        callback._record_task_result(result, 'ok')

        host_data = callback._current_task['hosts']['testhost']
        assert host_data['msg'] == 'done'
        assert (
            'end'
            in callback._current_task['task']['duration']
        )


# ── TestCustomStatsHostObjectKeys ────────────────────────────────


class TestCustomStatsHostObjectKeys:
    """Test the hasattr(k, 'get_name') branch in
    v2_playbook_on_stats (line 349)."""

    def test_custom_stats_with_host_object_keys(
        self, callback, tmp_path,
    ):
        """When custom stats keys are host objects with
        get_name(), use get_name() as the dict key."""
        callback.v2_playbook_on_play_start(_mock_play())

        host_obj = Mock()
        host_obj.get_name = Mock(return_value='web-1')

        custom = {
            host_obj: {'deploy_time': 42},
            '_run': {'total_deploys': 5},
        }
        stats = _mock_stats(
            host_summaries={
                'web-1': {
                    'ok': 5, 'changed': 0,
                    'unreachable': 0, 'failures': 0,
                    'skipped': 0, 'rescued': 0, 'ignored': 0,
                },
            },
            custom=custom,
        )
        callback.v2_playbook_on_stats(stats)

        with open(callback.json_log_path) as f:
            output = json.load(f)

        assert output['custom_stats'] == {
            'web-1': {'deploy_time': 42},
        }
        assert output['global_custom_stats'] == {
            'total_deploys': 5,
        }
        host_obj.get_name.assert_called_once()


# ── TestEmptyPlaybook ─────────────────────────────────────────────


class TestEmptyPlaybook:
    """Tests for edge case of an empty playbook (no plays, no tasks)."""

    def test_empty_playbook_produces_valid_json(
        self, callback, tmp_path,
    ):
        """A playbook with no plays still produces valid JSON
        with empty plays list and stats."""
        stats = _mock_stats(host_summaries={})
        stats.processed = {}
        callback.v2_playbook_on_stats(stats)

        assert os.path.exists(callback.json_log_path)

        with open(callback.json_log_path) as f:
            output = json.load(f)

        assert output['plays'] == []
        assert output['stats'] == {}


# ── TestFullLifecycle ─────────────────────────────────────────────


class TestFullLifecycle:
    """End-to-end test simulating a complete playbook run."""

    def test_multi_play_multi_task_run(self, callback, tmp_path):
        """Simulate a playbook with 2 plays, multiple tasks, a
        handler, an include, a retry, and a failure."""

        # Play 1: setup
        callback.v2_playbook_on_play_start(
            _mock_play(name='Setup', uuid='p1')
        )

        # Task 1: install (with loop)
        t1 = _mock_task(
            name='Install packages', uuid='t1', action='yum',
        )
        t1.loop = 'items'
        callback.v2_playbook_on_task_start(t1, is_conditional=False)
        callback.v2_runner_on_ok(_mock_result(
            host_name='host-1', task=t1,
            result_data={
                'changed': True,
                'results': [
                    {'item': 'httpd', 'changed': True},
                    {'item': 'php', 'changed': True},
                ],
            },
        ))

        # Include
        inc = Mock()
        inc._filename = '/roles/web/tasks/config.yml'
        host_mock = Mock()
        host_mock.name = 'host-1'
        inc._hosts = [host_mock]
        inc._vars = {}
        callback.v2_playbook_on_include(inc)

        # Task 2: configure
        t2 = _mock_task(
            name='Configure app', uuid='t2', action='template',
        )
        callback.v2_playbook_on_task_start(t2, is_conditional=False)
        callback.v2_runner_on_ok(_mock_result(
            host_name='host-1', task=t2,
            result_data={
                'changed': True,
                'diff': {
                    'before': 'old\n', 'after': 'new\n',
                },
            },
        ))

        # Handler: restart
        h1 = _mock_task(
            name='Restart httpd', uuid='h1', action='service',
        )
        callback.v2_playbook_on_handler_task_start(h1)
        callback.v2_runner_on_ok(_mock_result(
            host_name='host-1', task=h1,
            result_data={'changed': True},
        ))

        # Play 2: verify
        callback.v2_playbook_on_play_start(
            _mock_play(name='Verify', uuid='p2')
        )

        # Task 3: check (with retry then failure)
        t3 = _mock_task(
            name='Check service', uuid='t3', action='uri',
        )
        callback.v2_playbook_on_task_start(t3, is_conditional=False)
        callback.v2_runner_retry(_mock_result(
            host_name='host-1', task=t3,
            result_data={'attempts': 1, 'retries': 3},
        ))
        callback.v2_runner_on_failed(_mock_result(
            host_name='host-1', task=t3,
            result_data={
                'changed': False,
                'msg': 'Connection refused',
                'attempts': 2,
            },
        ))

        # Stats
        callback.v2_playbook_on_stats(_mock_stats({
            'host-1': {
                'ok': 3, 'changed': 2, 'unreachable': 0,
                'failures': 1, 'skipped': 0, 'rescued': 0,
                'ignored': 0,
            },
        }))

        # Verify JSON output
        with open(callback.json_log_path) as f:
            output = json.load(f)

        # Two plays
        assert len(output['plays']) == 2
        assert output['plays'][0]['play']['name'] == 'Setup'
        assert output['plays'][1]['play']['name'] == 'Verify'

        # Play 1: 2 tasks + 1 include + 1 handler = 4 entries
        p1_tasks = output['plays'][0]['tasks']
        assert len(p1_tasks) == 4

        # Task 1 has loop data
        assert 'results' in p1_tasks[0]['hosts']['host-1']

        # Include entry
        assert 'include' in p1_tasks[1]
        assert p1_tasks[1]['include']['file'] == (
            '/roles/web/tasks/config.yml'
        )

        # Task 2 has diff data
        assert 'diff' in p1_tasks[2]['hosts']['host-1']

        # Handler task
        assert p1_tasks[3]['task']['name'] == 'Restart httpd'

        # Play 2: 1 task with retry and failure
        p2_tasks = output['plays'][1]['tasks']
        assert len(p2_tasks) == 1
        assert p2_tasks[0]['hosts']['host-1']['failed'] is True
        assert 'retries' in p2_tasks[0]
        assert len(p2_tasks[0]['retries']['host-1']) == 1

        # Stats
        assert output['stats']['host-1']['failures'] == 1
