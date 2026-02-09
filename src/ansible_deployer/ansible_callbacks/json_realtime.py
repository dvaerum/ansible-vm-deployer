# (c) 2024, Ansible VM Manager
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""
Ansible callback plugin that provides both real-time output AND JSON logging.

This callback extends the default callback to provide real-time human-readable
output while also collecting structured data for JSON output at the end.

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
    short_description: Real-time output with EXACT Ansible JSON logging
    version_added: "2.0"
    description:
        - This callback provides real-time human-readable output while collecting
          structured data in Ansible JSON format
        - Combines benefits of default callback (real-time) and JSON output (structured)
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

class CallbackModule(DefaultCallbackModule):
    """
    Provides real-time output AND collects data for JSON logging.
    """

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stdout'
    CALLBACK_NAME = 'json_realtime'

    def __init__(self):
        # Call parent init first
        super(CallbackModule, self).__init__()
        
        # JSON output path from environment variable
        self.json_log_path = os.environ.get('JSON_LOG_PATH', './ansible_output.json')
        
        # Data collection for JSON output (Ansible JSON format)
        self.results = {
            'plays': [],
            'stats': {},
            'custom_stats': {}
        }
        self.current_play = None
        self.current_task = None
        self.task_results = []
        self.start_time = datetime.datetime.now()
        
    def _save_current_task(self):
        """Save current task to task_results if it has data."""
        if self.current_task and self.current_task['hosts']:
            self.current_task['task']['duration']['end'] = datetime.datetime.now().isoformat()
            self.task_results.append(self.current_task)
    
    def _save_current_play(self):
        """Save current play to results if it has data."""
        # First save any pending task
        self._save_current_task()
        
        if self.current_play:
            self.current_play['play']['duration']['end'] = datetime.datetime.now().isoformat()
            self.current_play['tasks'] = self.task_results
            self.results['plays'].append(self.current_play)

    def v2_playbook_on_play_start(self, play):
        """Play started - save previous play, start new one."""
        super(CallbackModule, self).v2_playbook_on_play_start(play)
        
        # Save previous play (if any)
        self._save_current_play()
        
        # Start new play
        self.current_play = {
            'play': {
                'name': play.get_name(),
                'id': str(play._uuid),
                'duration': {
                    'start': datetime.datetime.now().isoformat()
                }
            },
            'tasks': []
        }
        self.task_results = []

    def v2_playbook_on_task_start(self, task, is_conditional):
        """Task started - save previous task, start new one."""
        super(CallbackModule, self).v2_playbook_on_task_start(task, is_conditional)
        
        # Save previous task (if any)
        self._save_current_task()
        
        # Start new task
        self.current_task = {
            'task': {
                'name': task.get_name(),
                'id': str(task._uuid),
                'duration': {
                    'start': datetime.datetime.now().isoformat()
                }
            },
            'hosts': {}
        }

    def _record_task_result(self, result, status='ok'):
        """Record a task result for JSON output."""
        if self.current_task is None:
            return
            
        host = result._host.get_name()
        
        # Copy result data
        task_result = {}
        if hasattr(result, '_result'):
            for key, value in result._result.items():
                # Skip internal keys
                if not key.startswith('_ansible'):
                    task_result[key] = value
        
        # Add standard fields
        task_result['changed'] = result._result.get('changed', False)
        task_result['failed'] = status == 'failed'
        task_result['skipped'] = status == 'skipped'
        task_result['unreachable'] = status == 'unreachable'
        
        # Add action if available
        if hasattr(result._task, 'action'):
            task_result['action'] = result._task.action
        
        # Store in current task
        self.current_task['hosts'][host] = task_result

    def v2_runner_on_ok(self, result):
        """Task succeeded - call parent + record."""
        super(CallbackModule, self).v2_runner_on_ok(result)
        self._record_task_result(result, 'ok')

    def v2_runner_on_failed(self, result, ignore_errors=False):
        """Task failed - call parent + record."""
        super(CallbackModule, self).v2_runner_on_failed(result, ignore_errors)
        self._record_task_result(result, 'failed')

    def v2_runner_on_skipped(self, result):
        """Task skipped - call parent + record."""
        super(CallbackModule, self).v2_runner_on_skipped(result)
        self._record_task_result(result, 'skipped')

    def v2_runner_on_unreachable(self, result):
        """Host unreachable - call parent + record."""
        super(CallbackModule, self).v2_runner_on_unreachable(result)
        self._record_task_result(result, 'unreachable')
    
    def v2_runner_on_start(self, host, task):
        """Task starting on host - call parent."""
        # Only call parent if it has this method
        if hasattr(super(CallbackModule, self), 'v2_runner_on_start'):
            super(CallbackModule, self).v2_runner_on_start(host, task)
    
    def v2_runner_item_on_skipped(self, result):
        """Item skipped - call parent."""
        # Only call parent if it has this method
        if hasattr(super(CallbackModule, self), 'v2_runner_item_on_skipped'):
            super(CallbackModule, self).v2_runner_item_on_skipped(result)

    def v2_playbook_on_stats(self, stats):
        """Playbook ended - save final play and write JSON output."""
        super(CallbackModule, self).v2_playbook_on_stats(stats)
        
        # Save the last play (if any)
        self._save_current_play()
        
        # Collect final stats
        hosts = sorted(stats.processed.keys())
        for h in hosts:
            s = stats.summarize(h)
            self.results['stats'][h] = {
                'ok': s['ok'],
                'changed': s['changed'],
                'unreachable': s['unreachable'],
                'failed': s['failures'],
                'skipped': s['skipped'],
                'rescued': s['rescued'],
                'ignored': s['ignored']
            }
        
        # Write JSON to file
        try:
            with open(self.json_log_path, 'w') as f:
                json.dump(self.results, f, indent=2, default=str)
            self._display.display(u"\n[JSON output written to: %s]" % self.json_log_path, color='bright gray')
        except Exception as e:
            self._display.warning(u"Failed to write JSON log to %s: %s" % (self.json_log_path, str(e)))
