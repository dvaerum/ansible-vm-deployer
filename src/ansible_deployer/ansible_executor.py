"""
Ansible playbook executor with comprehensive logging.

Uses single-execution approach: runs playbook once with JSON output,
then post-processes JSON to create human-readable logs.
"""
import subprocess
import json
import logging
import os
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime
import shutil

from . import signal_handler


logger = logging.getLogger(__name__)


class AnsibleExecutionError(Exception):
    """Raised when Ansible execution fails."""
    pass


class AnsibleExecutor:
    """Executes Ansible playbooks with dual-format logging (single execution)."""

    def __init__(self, log_dir: Path = Path("./logs")):
        """Initialize Ansible executor.
        
        Args:
            log_dir: Directory to store logs
        """
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def execute_playbook(
        self,
        playbook_path: Path,
        extra_vars: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        vm_env_vars: Optional[Dict[str, str]] = None,
        inventory_path: Optional[Path] = None,
        wrapper_script_path: Optional[Path] = None,
        ansible_flags: Optional[str] = None,
        project_root: Optional[Path] = None,
        quiet: bool = False,
        passthrough_args: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        """Execute an Ansible playbook once, generating both JSON and human-readable logs.
        
        Args:
            playbook_path: Path to playbook file
            extra_vars: Extra variables to pass to Ansible
            task_id: Unique task identifier for logging
            vm_env_vars: VM environment variables to export (e.g., VM_IP_1, VM_IP_2, VM_IP_ALL)
            inventory_path: Optional path to Ansible inventory file
            wrapper_script_path: Optional path to wrapper script (if None, auto-detects or uses ansible-playbook directly)
            ansible_flags: Additional flags to pass to ansible-playbook (e.g., '--check --diff' or '-vvv')
            project_root: Optional project root directory (sets cwd for wrapper script execution)
            quiet: If True, suppress Ansible output to console (still writes to log files)
            passthrough_args: Additional arguments to pass through to wrapper script or ansible-playbook (appended at end)
            
        Returns:
            Dictionary with execution results
            
        Raises:
            AnsibleExecutionError: If execution fails
        """
        if not playbook_path.exists():
            raise FileNotFoundError(f"Playbook not found: {playbook_path}")

        # Generate task ID if not provided
        if task_id is None:
            task_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Prepare log files (task_id may contain subdirectories from --log-prefix)
        stdout_log = self.log_dir / f"{task_id}_stdout.log"
        json_log = self.log_dir / f"{task_id}.json"
        stdout_log.parent.mkdir(parents=True, exist_ok=True)

        # Build command using wrapper script or ansible-playbook directly
        # Priority:
        # 1. Use provided wrapper_script_path if specified
        # 2. Auto-detect wrapper in project root (legacy behavior)
        # 3. Fall back to ansible-playbook directly
        
        # Build base command
        if wrapper_script_path:
            # Use explicitly provided wrapper script
            if not wrapper_script_path.exists():
                raise FileNotFoundError(f"Wrapper script not found: {wrapper_script_path}")
            cmd = [str(wrapper_script_path), str(playbook_path)]
            logger.info(f"Using wrapper script: {wrapper_script_path}")
        else:
            # Try to auto-detect wrapper script (legacy behavior)
            auto_wrapper = (Path(__file__).parent.parent.parent / "ansible-wrapper.sh").resolve()
            if auto_wrapper.exists():
                cmd = [str(auto_wrapper), str(playbook_path)]
                logger.info(f"Auto-detected wrapper script: {auto_wrapper}")
            else:
                # Fall back to ansible-playbook directly
                if not shutil.which("ansible-playbook"):
                    raise RuntimeError("ansible-playbook command not found")
                cmd = ["ansible-playbook", str(playbook_path)]
                logger.info("Using ansible-playbook directly (no wrapper script)")
        
        # Add inventory file if provided
        if inventory_path:
            if not inventory_path.exists():
                raise FileNotFoundError(f"Inventory file not found: {inventory_path}")
            cmd.extend(["-i", str(inventory_path)])

        # Add extra vars
        if extra_vars:
            extra_vars_json = json.dumps(extra_vars)
            cmd.extend(["--extra-vars", extra_vars_json])
        
        # Add additional ansible flags if provided
        if ansible_flags:
            # Split the flags string and add to command
            import shlex
            additional_flags = shlex.split(ansible_flags)
            cmd.extend(additional_flags)
        
        # Add passthrough arguments (passed after -- in CLI)
        if passthrough_args:
            cmd.extend(passthrough_args)
            logger.info(f"Adding passthrough args: {' '.join(passthrough_args)}")

        # Prepare environment (merge with current env + VM vars)
        # This environment is passed to the wrapper script via subprocess.Popen(env=...)
        # which makes all variables (including VM_IP_1, VM_IP_2, VM_IP_ALL, etc.)
        # available in the wrapper script and to ansible-playbook
        process_env = {**os.environ}
        
        # Configure callback plugin for dual output (real-time + JSON)
        # Priority:
        # 1. User-specified ANSIBLE_CALLBACK_PLUGINS (allow override)
        # 2. Bundled callback plugin directory
        # 3. Fallback to Ansible's built-in json callback
        
        if 'ANSIBLE_CALLBACK_PLUGINS' not in process_env:
            # Use bundled callback plugin
            callback_dir = Path(__file__).parent / "ansible_callbacks"
            if callback_dir.exists():
                process_env['ANSIBLE_CALLBACK_PLUGINS'] = str(callback_dir)
                process_env['ANSIBLE_STDOUT_CALLBACK'] = 'json_realtime'
                process_env['JSON_LOG_PATH'] = str(json_log)
                logger.info("Using bundled json_realtime callback for dual output")
            else:
                # Fallback to Ansible's built-in json callback
                # Note: This means no real-time output, but you get valid JSON
                process_env['ANSIBLE_STDOUT_CALLBACK'] = 'json'
                logger.warning("Bundled callback not found, falling back to Ansible json callback (no real-time output)")
        else:
            # User has specified custom callback plugins directory
            # Try to use json_realtime if available
            process_env['ANSIBLE_STDOUT_CALLBACK'] = 'json_realtime'
            process_env['JSON_LOG_PATH'] = str(json_log)
            logger.info(f"Using user-specified callback plugins: {process_env['ANSIBLE_CALLBACK_PLUGINS']}")
        
        # Add VM environment variables if provided (VM_IP_1, VM_IP_2, VM_IP_ALL, etc.)
        if vm_env_vars:
            process_env.update(vm_env_vars)

        logger.info(f"Executing playbook: {playbook_path}")
        logger.info(f"Command: {' '.join(cmd)}")
        if project_root:
            logger.info(f"Working directory: {project_root}")
        if vm_env_vars:
            for key, value in vm_env_vars.items():
                logger.info(f"Environment: {key}={value}")

        result = {
            "task_id": task_id,
            "playbook": str(playbook_path),
            "start_time": datetime.now().isoformat(),
            "success": False,
            "stdout_log": str(stdout_log),
            "json_log": str(json_log),
        }

        stdout_log_file = None
        process = None
        try:
            # Open stdout log file for real-time writing
            # Since we're not using JSON callback, output is human-readable by default
            stdout_log_file = open(stdout_log, 'w')
            stdout_log_file.write(f"=== ANSIBLE PLAYBOOK EXECUTION ===\n")
            stdout_log_file.write(f"Task ID: {task_id}\n")
            stdout_log_file.write(f"Playbook: {playbook_path}\n")
            stdout_log_file.write(f"Started: {datetime.now().isoformat()}\n")
            stdout_log_file.write("=" * 80 + "\n\n")
            stdout_log_file.flush()
            
            # Execute playbook and capture output
            all_output = []
            
            # Set working directory to project_root if provided
            # This allows wrapper scripts to use relative paths
            working_dir = str(project_root) if project_root else None
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=process_env,
                cwd=working_dir,
            )
            signal_handler.register_subprocess(process)

            # Capture output line by line and write to stdout log in REAL-TIME
            if process.stdout:
                for line in process.stdout:
                    all_output.append(line)
                    
                    # Only print to console if not in quiet mode
                    if not quiet:
                        logger.info(line.rstrip())
                    
                    # Always write to stdout log file in real-time for monitoring
                    stdout_log_file.write(line)
                    stdout_log_file.flush()  # Ensure it's written immediately

            return_code = process.wait()
            signal_handler.deregister_subprocess()
            
            # Add completion info
            stdout_log_file.write(f"\n{'=' * 80}\n")
            stdout_log_file.write(f"Completed: {datetime.now().isoformat()}\n")
            stdout_log_file.write(f"Return Code: {return_code}\n")

            # Note: JSON log is written by the callback plugin
            # The callback plugin writes EXACT Ansible JSON format to json_log path
            # No need to create a separate JSON file here

            result["end_time"] = datetime.now().isoformat()
            result["return_code"] = return_code
            result["success"] = return_code == 0

            # Verify JSON log was created by callback
            if not json_log.exists():
                logger.warning(f"JSON log was not created by callback plugin: {json_log}")
                logger.warning("This might indicate callback plugin failed to load")

            if return_code != 0:
                logger.error(f"Playbook execution failed with return code {return_code}")
            else:
                logger.info("Playbook execution completed successfully")

            return result

        except Exception as e:
            result["end_time"] = datetime.now().isoformat()
            result["error"] = str(e)
            logger.exception("Playbook execution error")
            
            # Write error to stdout log file if it's open
            if stdout_log_file and not stdout_log_file.closed:
                try:
                    stdout_log_file.write(f"\n{'=' * 80}\n")
                    stdout_log_file.write(f"ERROR: {str(e)}\n")
                    stdout_log_file.write(f"Failed: {datetime.now().isoformat()}\n")
                except:
                    pass  # Ignore errors while writing error message
            
            raise AnsibleExecutionError(f"Failed to execute playbook: {e}") from e
        
        finally:
            # Ensure child process is terminated and reaped to avoid
            # orphans. This runs on normal exit, exceptions, and
            # signal-triggered SystemExit/KeyboardInterrupt.
            signal_handler.terminate_active_subprocess()
            signal_handler.deregister_subprocess()
            if process is not None and process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

            # Always close the stdout log file
            if stdout_log_file and not stdout_log_file.closed:
                try:
                    stdout_log_file.close()
                except:
                    pass  # Ignore errors while closing

    def _write_human_readable_log(self, json_data: Dict, output_path: Path, warnings: str = ""):
        """Convert JSON playbook output to human-readable format.
        
        Args:
            json_data: Parsed JSON output from Ansible
            output_path: Path to write human-readable log
            warnings: Any warnings/messages that appeared before the JSON
        """
        with open(output_path, 'w') as f:
            # Write warnings first if present
            if warnings:
                f.write(warnings + "\n\n")
            
            # Header
            f.write("=" * 80 + "\n")
            f.write("ANSIBLE PLAYBOOK EXECUTION\n")
            f.write("=" * 80 + "\n\n")

            # Process each play
            for play in json_data.get('plays', []):
                play_name = play.get('play', {}).get('name', 'Unnamed play')
                f.write(f"PLAY [{play_name}] ")
                f.write("*" * (79 - len(play_name) - 8) + "\n\n")

                # Process each task in the play
                for task in play.get('tasks', []):
                    task_info = task.get('task', {})
                    task_name = task_info.get('name', 'Unnamed task')
                    
                    f.write(f"TASK [{task_name}] ")
                    f.write("*" * (79 - len(task_name) - 8) + "\n")

                    # Process results for each host
                    hosts = task.get('hosts', {})
                    for host, host_result in hosts.items():
                        # Determine status
                        if host_result.get('failed', False):
                            status = 'fatal'
                        elif host_result.get('unreachable', False):
                            status = 'unreachable'
                        elif host_result.get('changed', False):
                            status = 'changed'
                        elif host_result.get('skipped', False):
                            status = 'skipped'
                        else:
                            status = 'ok'

                        # Write status line
                        f.write(f"{status}: [{host}]")

                        # Add message if present
                        if host_result.get('msg'):
                            msg = host_result['msg']
                            # Handle multi-line messages
                            if isinstance(msg, str) and '\n' in msg:
                                f.write(" => \n")
                                for msg_line in msg.split('\n'):
                                    f.write(f"  {msg_line}\n")
                            else:
                                f.write(f" => {msg}\n")
                        else:
                            f.write("\n")

                        # Add details for failures
                        if host_result.get('failed', False):
                            if host_result.get('stderr'):
                                f.write(f"  stderr: {host_result['stderr']}\n")
                            if host_result.get('rc'):
                                f.write(f"  rc: {host_result['rc']}\n")

                    f.write("\n")

            # Write play recap
            stats = json_data.get('stats', {})
            if stats:
                f.write("PLAY RECAP ")
                f.write("*" * 70 + "\n")

                for host, host_stats in stats.items():
                    f.write(f"{host:<30} : ")
                    f.write(f"ok={host_stats.get('ok', 0):<4} ")
                    f.write(f"changed={host_stats.get('changed', 0):<4} ")
                    f.write(f"unreachable={host_stats.get('unreachable', 0):<4} ")
                    f.write(f"failed={host_stats.get('failures', 0):<4} ")
                    f.write(f"skipped={host_stats.get('skipped', 0):<4} ")
                    f.write(f"rescued={host_stats.get('rescued', 0):<4} ")
                    f.write(f"ignored={host_stats.get('ignored', 0):<4}\n")

                f.write("\n")

    def get_log_contents(self, task_id: str, log_type: str = "stdout") -> str:
        """Get log contents for a task.
        
        Args:
            task_id: Task identifier
            log_type: Type of log ("stdout" or "json")
            
        Returns:
            Log contents
        """
        if log_type == "json":
            log_file = self.log_dir / f"{task_id}.json"
        else:
            log_file = self.log_dir / f"{task_id}_{log_type}.log"
            
        if not log_file.exists():
            raise FileNotFoundError(f"Log file not found: {log_file}")

        return log_file.read_text()

    def list_logs(self) -> list[Dict[str, str]]:
        """List all available logs.
        
        Returns:
            List of log information
        """
        logs = []
        for log_file in sorted(self.log_dir.glob("*_stdout.log"), reverse=True):
            task_id = log_file.stem.replace("_stdout", "")
            json_log = self.log_dir / f"{task_id}.json"
            logs.append({
                "task_id": task_id,
                "stdout_log": str(log_file),
                "json_log": str(json_log) if json_log.exists() else "",
                "timestamp": datetime.fromtimestamp(
                    log_file.stat().st_mtime
                ).isoformat(),
            })
        return logs