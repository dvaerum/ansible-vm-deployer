# (c) 2024, Ansible VM Manager
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""
Ansible callback plugin that provides both real-time output AND JSON logging.

This callback extends the default callback to provide real-time human-readable
output while also collecting structured data for JSON output at the end.

JSON format matches the official ansible.posix.json callback structure:
- plays[].play: name, id, path, duration
- plays[].tasks[].task: name, id, path, duration
- plays[].tasks[].hosts: per-host result data
- stats: per-host summary (ok, changed, unreachable, failures, skipped, rescued, ignored)

Additionally records events not captured by the official callback:
- Handler tasks (v2_playbook_on_handler_task_start)
- Include events (v2_playbook_on_include)
- Retry attempts (v2_runner_retry)

Configuration:
- Set JSON_LOG_PATH environment variable to specify where to write JSON
- If not set, JSON is written to ./ansible_output.json

Usage:
    export ANSIBLE_STDOUT_CALLBACK=json_realtime
    export ANSIBLE_CALLBACK_PLUGINS=/path/to/callbacks
    export JSON_LOG_PATH=/path/to/output.json
    ansible-playbook playbook.yml
"""

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = '''
    callback: json_realtime
    type: stdout
    short_description: Real-time output with structured JSON logging
    version_added: "2.0"
    description:
        - This callback provides real-time human-readable output while
          collecting structured data in Ansible JSON format
        - Combines benefits of default callback (real-time) and JSON
          output (structured)
        - JSON format matches the official ansible.posix.json callback
    extends_documentation_fragment:
      - default_callback
      - result_format_callback
    requirements:
      - Set JSON_LOG_PATH environment variable to specify JSON output location
'''

import os
import json
import datetime
from ansible.plugins.callback.default import CallbackModule as DefaultCallbackModule


def _current_time():
    """Return current UTC time in ISO format with Z suffix.

    Matches the timestamp format used by the official
    ansible.posix.json callback.

    Uses datetime.now(UTC) instead of the deprecated utcnow().
    The tzinfo is stripped before isoformat() so the output is
    a bare ISO timestamp with Z suffix (no +00:00 offset).
    """
    utc_now = datetime.datetime.now(datetime.UTC)
    return '%sZ' % utc_now.replace(tzinfo=None).isoformat()


class CallbackModule(DefaultCallbackModule):
    """Provides real-time terminal output AND structured JSON logging.

    Real-time output is handled by the parent DefaultCallbackModule
    via super() calls. This class additionally collects structured
    data and writes it as JSON at playbook completion.

    IMPORTANT: For v2_runner_on_ok/failed/skipped/unreachable, JSON
    recording happens BEFORE calling super(). The parent's
    _process_items() deletes result._result['results'] for loop
    tasks, which would cause per-item data to be lost in the JSON
    if we recorded after.
    """

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stdout'
    CALLBACK_NAME = 'json_realtime'

    def __init__(self):
        super(CallbackModule, self).__init__()

        self.json_log_path = os.environ.get(
            'JSON_LOG_PATH', './ansible_output.json'
        )

        # Structured data collected during playbook execution.
        # Uses direct-append pattern: plays and tasks are added to
        # the list immediately on start, not deferred until the next
        # event. This avoids the fragile "save on next start" pattern
        # which can lose the last task/play if interrupted.
        self.results = []
        self._current_play = None
        self._current_task = None

    # ── Helpers ────────────────────────────────────────────────────

    def _new_play(self, play):
        """Create a new play data structure for JSON output.

        Includes 'path' field matching the official ansible.posix.json
        callback format (file:line of the play definition).
        """
        return {
            'play': {
                'name': play.get_name(),
                'id': str(play._uuid),
                'path': play.get_path(),
                'duration': {
                    'start': _current_time(),
                },
            },
            'tasks': [],
        }

    def _new_task(self, task):
        """Create a new task data structure for JSON output.

        Includes 'path' field matching the official ansible.posix.json
        callback format (file:line of the task definition).
        """
        return {
            'task': {
                'name': task.get_name(),
                'id': str(task._uuid),
                'path': task.get_path(),
                'duration': {
                    'start': _current_time(),
                },
            },
            'hosts': {},
        }

    def _record_task_result(
        self, result, status='ok', ignore_errors=False,
    ):
        """Record a task result for JSON output.

        Copies result data into a new dict, filtering out internal
        _ansible_* keys but preserving all task output (stdout, stderr,
        diff, results for loops, etc.).

        This MUST be called BEFORE super() for runner events, because
        the parent's _process_items() deletes result._result['results']
        for loop tasks — recording after super() would lose per-item
        data in the JSON output.
        """
        if self._current_task is None:
            return

        host = result._host.get_name()

        # Build result dict, filtering internal Ansible keys
        task_result = {}
        for key, value in result._result.items():
            if not key.startswith('_ansible'):
                task_result[key] = value

        # Ensure standard status fields are always present
        task_result['changed'] = result._result.get('changed', False)
        task_result['failed'] = status == 'failed'
        task_result['skipped'] = status == 'skipped'
        task_result['unreachable'] = status == 'unreachable'

        # Record whether a failure was ignored via ignore_errors
        if ignore_errors:
            task_result['ignore_errors'] = True

        # Add task action (e.g., 'yum', 'copy', 'shell')
        if hasattr(result._task, 'action'):
            task_result['action'] = result._task.action

        self._current_task['hosts'][host] = task_result

        # Update duration timestamps
        end_time = _current_time()
        self._current_task['task']['duration']['end'] = end_time
        if self._current_play:
            self._current_play['play']['duration']['end'] = end_time

    # ── Play lifecycle ─────────────────────────────────────────────

    def v2_playbook_on_play_start(self, play):
        """Play started — append new play to results."""
        super(CallbackModule, self).v2_playbook_on_play_start(play)
        self._current_play = self._new_play(play)
        self.results.append(self._current_play)

    # ── Task lifecycle ─────────────────────────────────────────────

    def v2_playbook_on_task_start(self, task, is_conditional):
        """Task started — append new task to current play."""
        super(CallbackModule, self).v2_playbook_on_task_start(
            task, is_conditional
        )
        self._current_task = self._new_task(task)
        if self._current_play:
            self._current_play['tasks'].append(self._current_task)

    def v2_playbook_on_handler_task_start(self, task):
        """Handler task started — record in JSON like regular tasks.

        Without this override, handler tasks are displayed in the
        terminal (via the parent) but missing from the JSON output.
        """
        super(CallbackModule, self).v2_playbook_on_handler_task_start(
            task
        )
        self._current_task = self._new_task(task)
        if self._current_play:
            self._current_play['tasks'].append(self._current_task)

    def v2_playbook_on_include(self, included_file):
        """Include event — record which file was included and for
        which hosts.

        Not tracked by the official JSON callback, but useful for
        tracing task origin in complex playbooks with many includes.
        Recorded as a lightweight entry in the play's tasks list.
        """
        super(CallbackModule, self).v2_playbook_on_include(
            included_file
        )
        if self._current_play is not None:
            include_entry = {
                'include': {
                    'file': str(included_file._filename),
                    'hosts': [
                        h.name for h in included_file._hosts
                    ],
                },
            }
            label = self._get_item_label(included_file._vars)
            if label:
                include_entry['include']['item'] = label
            self._current_play['tasks'].append(include_entry)

    # ── Runner results ─────────────────────────────────────────────
    #
    # Record BEFORE super() — the parent's _process_items() deletes
    # result._result['results'] for loop tasks. Recording after
    # would lose per-item data in the JSON output.

    def v2_runner_on_ok(self, result):
        """Task succeeded — record to JSON, then display."""
        self._record_task_result(result, 'ok')
        super(CallbackModule, self).v2_runner_on_ok(result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        """Task failed — record to JSON, then display.

        Records ignore_errors flag so JSON consumers can distinguish
        between fatal failures and ignored ones.
        """
        self._record_task_result(
            result, 'failed', ignore_errors=ignore_errors,
        )
        super(CallbackModule, self).v2_runner_on_failed(
            result, ignore_errors
        )

    def v2_runner_on_skipped(self, result):
        """Task skipped — record to JSON, then display."""
        self._record_task_result(result, 'skipped')
        super(CallbackModule, self).v2_runner_on_skipped(result)

    def v2_runner_on_unreachable(self, result):
        """Host unreachable — record to JSON, then display."""
        self._record_task_result(result, 'unreachable')
        super(CallbackModule, self).v2_runner_on_unreachable(result)

    def v2_runner_on_start(self, host, task):
        """Task starting on host — forward to parent for display."""
        if hasattr(
            super(CallbackModule, self), 'v2_runner_on_start'
        ):
            super(CallbackModule, self).v2_runner_on_start(host, task)

    def v2_runner_item_on_skipped(self, result):
        """Loop item skipped — forward to parent for display.

        Individual loop items are not separately recorded in JSON.
        The aggregate result (with per-item data in 'results' list)
        is captured by v2_runner_on_ok/failed/skipped.
        """
        if hasattr(
            super(CallbackModule, self), 'v2_runner_item_on_skipped'
        ):
            super(CallbackModule, self).v2_runner_item_on_skipped(
                result
            )

    def v2_runner_retry(self, result):
        """Task retry — record attempt count per host.

        Not tracked by the official JSON callback, but useful for
        diagnosing flaky tasks. Each retry attempt is recorded with
        the current attempt number and total retries configured.
        """
        super(CallbackModule, self).v2_runner_retry(result)
        if self._current_task is not None:
            host = result._host.get_name()
            retry_info = {
                'attempts': result._result.get('attempts', 0),
                'retries': result._result.get('retries', 0),
            }
            if 'retries' not in self._current_task:
                self._current_task['retries'] = {}
            if host not in self._current_task['retries']:
                self._current_task['retries'][host] = []
            self._current_task['retries'][host].append(retry_info)

    # ── Playbook completion ────────────────────────────────────────

    def v2_playbook_on_stats(self, stats):
        """Playbook ended — write JSON output to file.

        Stats format matches the official ansible.posix.json callback:
        per-host dict from stats.summarize() with keys ok, changed,
        unreachable, failures (not 'failed'), skipped, rescued, ignored.
        """
        super(CallbackModule, self).v2_playbook_on_stats(stats)

        # Collect per-host stats using Ansible's summarize() as-is,
        # matching the official ansible.posix.json format
        hosts = sorted(stats.processed.keys())
        summary = {}
        for h in hosts:
            summary[h] = stats.summarize(h)

        # Collect custom stats (per-host and global)
        custom_stats = {}
        global_custom_stats = {}
        if stats.custom:
            for k, v in stats.custom.items():
                if k == '_run':
                    global_custom_stats.update(v)
                elif hasattr(k, 'get_name'):
                    custom_stats[k.get_name()] = v
                else:
                    custom_stats[str(k)] = v

        output = {
            'plays': self.results,
            'stats': summary,
            'custom_stats': custom_stats,
            'global_custom_stats': global_custom_stats,
        }

        try:
            with open(self.json_log_path, 'w') as f:
                json.dump(output, f, indent=2, default=str)
            self._display.display(
                u"\n[JSON output written to: %s]"
                % self.json_log_path,
                color='bright gray',
            )
        except Exception as e:
            self._display.warning(
                u"Failed to write JSON log to %s: %s"
                % (self.json_log_path, str(e))
            )
