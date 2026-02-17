"""
Command-line interface for ansible-deployer.
"""
import click
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional, List
from rich.console import Console
from rich.table import Table
from rich import print as rprint
from datetime import datetime
import uuid
import time

from .config import Config
from .vm_manager import VMManager, VMNotFoundException, NoAvailableVMException
from .ansible_executor import AnsibleExecutor, AnsibleExecutionError
from .metadata_manager import MetadataManager
from .vm_reset import VMResetManager, VMResetError


console = Console()


def sanitize_log_prefix(prefix: str) -> str:
    """Sanitize a log prefix string for use in file paths.

    Allows alphanumeric characters, hyphens, underscores, and forward slashes
    (for subdirectory support). All other characters are replaced with hyphens.
    Repeated slashes are collapsed and leading/trailing slashes are stripped.

    Args:
        prefix: Raw prefix string from --log-prefix

    Returns:
        Sanitized prefix safe for use in file paths
    """
    sanitized = "".join(c if c.isalnum() or c in "-_/" else "-" for c in prefix)
    sanitized = re.sub(r"/+", "/", sanitized).strip("/")
    return sanitized


def setup_logging(level: str = "INFO") -> None:
    """Setup logging configuration.
    
    Args:
        level: Logging level
    """
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("ansible-deployer.log"),
        ],
    )


@click.group()
@click.option(
    "--config",
    type=click.Path(exists=True, path_type=Path),
    help="Path to configuration file",
)
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Project root directory (playbook, inventory, and log paths are relative to this)",
)
@click.option(
    "--log-dir",
    type=click.Path(path_type=Path),
    help="Log directory (relative to project-root if set, default: ./logs)",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default=None,
    help="Logging level (default: INFO)",
)
@click.option("--verbose", "-v", count=True, help="Increase verbosity")
@click.pass_context
def main(ctx: click.Context, config: Optional[Path], project_root: Optional[Path], log_dir: Optional[Path], log_level: Optional[str], verbose: int) -> None:
    """Ansible Deployer - Deploy playbooks to libvirt VMs with automatic cleanup."""
    # Load configuration
    cfg = Config.load(config)
    
    # Determine log level: --verbose overrides --log-level, which overrides default
    if verbose >= 2:
        effective_log_level = "DEBUG"
    elif verbose == 1:
        effective_log_level = "DEBUG"
    elif log_level:
        effective_log_level = log_level.upper()
    else:
        effective_log_level = "INFO"
    
    setup_logging(effective_log_level)
    
    # Resolve log directory
    resolved_project_root = project_root.resolve() if project_root else None
    
    if log_dir:
        # Custom log directory specified
        if log_dir.is_absolute():
            log_directory = log_dir
        elif resolved_project_root:
            log_directory = (resolved_project_root / log_dir).resolve()
        else:
            log_directory = log_dir.resolve()
    elif resolved_project_root:
        # Use project_root/logs
        log_directory = (resolved_project_root / "logs").resolve()
    else:
        # Default
        log_directory = Path("./logs")
    
    # Store config, project root, and log directory in context
    ctx.ensure_object(dict)
    ctx.obj["config"] = cfg
    ctx.obj["project_root"] = resolved_project_root
    ctx.obj["log_directory"] = log_directory


@main.command()
@click.option("--tag", multiple=True, required=True, help="VM tag(s) to match (VM must have at least one)")
@click.option("--exclude-tag", multiple=True, help="VM tag(s) to exclude (VM must have none of these)")
@click.option("--playbook", type=click.Path(path_type=Path), required=True, help="Path to Ansible playbook (relative to project-root if set)")
@click.option("--inventory", type=click.Path(path_type=Path), help="Path to Ansible inventory file (relative to project-root if set)")
@click.option("--extra-vars", help="Extra variables as JSON")
@click.option("--no-reset", is_flag=True, help="Skip VM reset after execution")
@click.option("--network", help="Libvirt network name to use for IP discovery (e.g., 'mgmt-network'). If not specified, uses first interface with IP or config default.")
@click.option("--vm-count", default=1, type=int, help="Number of VMs to allocate (default: 1)")
@click.option("--allocation-timeout", type=int, help="Timeout in seconds for VM allocation (default: infinite retry)")
@click.option("--ansible-flags", help="Additional flags to pass to ansible-playbook (e.g., '--check --diff' or '-vvv')")
@click.option("--log-prefix", help="Prefix for log filenames. Supports subdirectories (e.g., 'test/linux' creates logs/test/linux_<timestamp>_stdout.log)")
@click.option("--repeat", default=1, type=click.IntRange(min=1), metavar="N", help="Number of times to execute the playbook (default: 1). Runs on the same VM without reset between iterations. Stops on first failure.")
@click.option("--quiet", is_flag=True, help="Suppress Ansible output to console (still writes to log files)")
@click.option("--mark-in-use", "mark_in_use_tag", is_flag=False, flag_value="used", default=None, help="Add usage tag to VM description after allocation (default: 'used' if no value given)")
@click.option("--mark-available", is_flag=True, help="Remove usage tag from VM description after reset")
@click.argument("passthrough_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def deploy(
    ctx: click.Context,
    tag: tuple[str, ...],
    exclude_tag: tuple[str, ...],
    playbook: Path,
    inventory: Optional[Path],
    extra_vars: Optional[str],
    no_reset: bool,
    network: Optional[str],
    vm_count: int,
    allocation_timeout: Optional[int],
    ansible_flags: Optional[str],
    log_prefix: Optional[str],
    repeat: int,
    quiet: bool,
    mark_in_use_tag: Optional[str],
    mark_available: bool,
    passthrough_args: tuple[str, ...],
) -> None:
    """Deploy Ansible playbook with allocated VMs.
    
    Pass additional arguments to the wrapper script or ansible-playbook by adding them after '--':
    
        deploy --tag test --playbook foo.yml -- --custom-flag --another-arg value
    
    These arguments are appended to the end of the command.
    """
    cfg: Config = ctx.obj["config"]
    project_root: Optional[Path] = ctx.obj.get("project_root")
    log_directory: Path = ctx.obj["log_directory"]
    
    # Helper function to resolve paths relative to project root
    def resolve_path(path: Path, must_exist: bool = True) -> Path:
        """Resolve path relative to project root if set and path is not absolute."""
        if path.is_absolute():
            resolved = path
        elif project_root:
            resolved = (project_root / path).resolve()
        else:
            resolved = path.resolve()
        
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"Path not found: {resolved}")
        return resolved
    
    # Resolve paths relative to project root
    playbook = resolve_path(playbook, must_exist=True)
    
    if inventory:
        inventory = resolve_path(inventory, must_exist=True)
    
    # Resolve wrapper script path
    # Only pass wrapper_script_path if the file actually exists
    # Otherwise, let the executor use its fallback logic
    if project_root:
        potential_wrapper = (project_root / "ansible-wrapper.sh").resolve()
        wrapper_script_path = potential_wrapper if potential_wrapper.exists() else None
    else:
        wrapper_script_path = None
    
    selected_network = network or None
    
    # Generate task_id with optional prefix
    base_task_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + str(uuid.uuid4())[:8]
    if log_prefix:
        sanitized_prefix = sanitize_log_prefix(log_prefix)
        task_id = f"{sanitized_prefix}_{base_task_id}"
    else:
        task_id = base_task_id
    
    console.print(f"[bold blue]Starting deployment task: {task_id}[/bold blue]")
    if project_root:
        console.print(f"Project root: {project_root}")
    console.print(f"Tags: {', '.join(tag)}")
    if exclude_tag:
        console.print(f"Exclude tags: {', '.join(exclude_tag)}")
    console.print(f"VM count: {vm_count}")
    if repeat > 1:
        console.print(f"Repeat: {repeat} iterations")
    console.print(f"Playbook: {playbook}")
    console.print(f"Log directory: {log_directory}")
    
    vm_manager = VMManager(cfg.libvirt_uri)
    executor = AnsibleExecutor(log_directory)
    reset_manager = VMResetManager()
    
    domains: List = []
    success = False
    start_time = time.time()
    
    try:
        with vm_manager:
            # Find and atomically claim VMs with wait/retry logic
            console.print(f"[yellow]Searching for {vm_count} available VM(s)...[/yellow]")
            
            while True:
                domains = vm_manager.allocate_vms(
                    list(tag), 
                    vm_count,
                    task_id=task_id,
                    exclude_tags=list(exclude_tag) if exclude_tag else None
                )
                
                if len(domains) >= vm_count:
                    break
                
                # Release any partially claimed VMs before retrying
                for domain in domains:
                    MetadataManager(domain).mark_available()
                domains = []
                
                # Check timeout BEFORE waiting
                if allocation_timeout is not None:
                    elapsed = time.time() - start_time
                    if elapsed >= allocation_timeout:
                        if exclude_tag:
                            raise NoAvailableVMException(
                                f"Timeout: Could not allocate {vm_count} VMs with tags: {tag} (excluding: {exclude_tag})"
                            )
                        else:
                            raise NoAvailableVMException(
                                f"Timeout: Could not allocate {vm_count} VMs with tags: {tag}"
                            )
                
                # Wait and retry (but don't wait if it would exceed timeout)
                console.print(f"[yellow]Found fewer than {vm_count} available VMs. Waiting 60 seconds before retry...[/yellow]")
                
                if allocation_timeout is not None:
                    remaining = allocation_timeout - (time.time() - start_time)
                    if remaining > 0:
                        wait_time = min(60, remaining)
                        time.sleep(wait_time)
                    # Will check timeout again at start of next loop
                else:
                    time.sleep(60)
            
            # VMs are already claimed by allocate_vms() - collect info
            vm_names = []
            vm_ips = []
            
            for i, domain in enumerate(domains, start=1):
                vm_name = domain.name()
                vm_names.append(vm_name)
                console.print(f"[green]Selected VM {i}: {vm_name}[/green]")
                
                # Add usage tag to VM description if requested
                if mark_in_use_tag:
                    vm_manager.add_vm_tag(domain, mark_in_use_tag)
                    console.print(f"  [dim]Added tag '{mark_in_use_tag}' to VM description[/dim]")
                
                # Get VM IP address
                vm_ip = vm_manager.get_vm_ip(domain, network=selected_network)
                if vm_ip is None:
                    if selected_network:
                        raise RuntimeError(f"Could not determine IP address for {vm_name} on network {selected_network}")
                    else:
                        raise RuntimeError(f"Could not determine IP address for {vm_name}")
                
                vm_ips.append(vm_ip)
                console.print(f"  VM {i} IP: {vm_ip}")
            
            console.print(f"[yellow]All {vm_count} VM(s) marked as in use[/yellow]")
            
            # Build environment variables
            vm_env_vars = {}
            for i, ip in enumerate(vm_ips, start=1):
                vm_env_vars[f"VM_IP_{i}"] = ip
            vm_env_vars["VM_IP_ALL"] = ",".join(vm_ips)
            
            # Parse extra vars
            import json
            extra_vars_dict = json.loads(extra_vars) if extra_vars else {}
            
            # Execute playbook (with optional repeat)
            if wrapper_script_path:
                console.print(f"Wrapper script: {wrapper_script_path}")
            
            for run_num in range(1, repeat + 1):
                # Build run-specific task_id: add _runN suffix when repeat > 1
                if repeat > 1:
                    run_task_id = f"{task_id}_run-{run_num}"
                    console.print(f"[yellow]Executing playbook (run {run_num}/{repeat})...[/yellow]")
                else:
                    run_task_id = task_id
                    console.print("[yellow]Executing playbook...[/yellow]")
                
                # Show log file location so user can monitor progress in real-time
                real_time_log = log_directory / f"{run_task_id}_stdout.log"
                console.print(f"[dim]Monitor progress: tail -f {real_time_log}[/dim]")
                
                result = executor.execute_playbook(
                    playbook_path=playbook,
                    extra_vars=extra_vars_dict,
                    task_id=run_task_id,
                    vm_env_vars=vm_env_vars,
                    inventory_path=inventory,
                    wrapper_script_path=wrapper_script_path,
                    ansible_flags=ansible_flags,
                    project_root=project_root,
                    quiet=quiet,
                    passthrough_args=list(passthrough_args) if passthrough_args else None,
                )
                
                success = result["success"]
                
                console.print(f"Logs saved to:")
                console.print(f"  - stdout: {result['stdout_log']}")
                console.print(f"  - json: {result['json_log']}")
                
                if not success:
                    if repeat > 1:
                        console.print(f"[bold red]Playbook execution failed on run {run_num}/{repeat}![/bold red]")
                    else:
                        console.print("[bold red]Playbook execution failed![/bold red]")
                    break
                
                if repeat > 1:
                    console.print(f"[bold green]Run {run_num}/{repeat} succeeded[/bold green]")
            
            if success:
                if repeat > 1:
                    console.print(f"[bold green]All {repeat} runs completed successfully![/bold green]")
                else:
                    console.print("[bold green]Playbook executed successfully![/bold green]")
            
    except NoAvailableVMException as e:
        console.print(f"[bold red]Error: {e}[/bold red]")
        sys.exit(1)
    except AnsibleExecutionError as e:
        console.print(f"[bold red]Ansible execution failed: {e}[/bold red]")
        success = False
    except RuntimeError as e:
        # User-friendly errors (authentication, connection, etc.)
        console.print(f"[bold red]Error: {e}[/bold red]")
        sys.exit(1)
    except Exception as e:
        # Unexpected errors - log full traceback
        console.print(f"[bold red]Unexpected error: {e}[/bold red]")
        logging.exception("Deployment error")
        sys.exit(1)
    finally:
        # Always reset VMs and mark as available (unless --no-reset)
        if domains:
            console.print(f"[yellow]Cleaning up {len(domains)} VM(s)...[/yellow]")
            for domain in domains:
                metadata_mgr = MetadataManager(domain)
                try:
                    if not no_reset:
                        console.print(f"[yellow]Resetting VM: {domain.name()}...[/yellow]")
                        with vm_manager:
                            reset_manager.reset_vm(domain)
                        console.print(f"[green]VM {domain.name()} reset complete[/green]")
                    
                    # Remove usage tag if --mark-available was specified
                    if mark_available and mark_in_use_tag:
                        with vm_manager:
                            vm_manager.remove_vm_tag(domain, mark_in_use_tag)
                        console.print(f"  [dim]Removed tag '{mark_in_use_tag}' from VM description[/dim]")
                    
                    # Mark VM as available
                    metadata_mgr.mark_available()
                    console.print(f"[green]VM {domain.name()} marked as available[/green]")
                    
                except VMResetError as e:
                    console.print(f"[bold red]Failed to reset VM {domain.name()}: {e}[/bold red]")
                    # Still mark as available
                    metadata_mgr.mark_available()
    
    if not success:
        sys.exit(1)


@main.command()
@click.option("--json", "output_json", is_flag=True, default=False, help="Output in JSON format")
@click.pass_context
def list_vms(ctx: click.Context, output_json: bool) -> None:
    """List all VMs and their status."""
    cfg: Config = ctx.obj["config"]
    
    vm_manager = VMManager(cfg.libvirt_uri)
    
    with vm_manager:
        vms = vm_manager.list_vms()
    
    if output_json:
        click.echo(json.dumps(vms, indent=2))
        return
    
    if not vms:
        console.print("[yellow]No VMs found[/yellow]")
        return
    
    table = Table(title="Virtual Machines")
    table.add_column("Name", style="cyan")
    table.add_column("UUID", style="magenta")
    table.add_column("State", style="green")
    table.add_column("Tags", style="dim")
    table.add_column("In Use", style="yellow")
    table.add_column("Task ID")
    
    for vm in vms:
        table.add_row(
            vm["name"],
            vm["uuid"],
            vm["state"],
            ", ".join(vm["tags"]) if vm["tags"] else "",
            str(vm["in_use"]),
            vm["task_id"],
        )
    
    console.print(table)


@main.command()
@click.option("--vm-name", required=True, help="VM name")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output in JSON format")
@click.pass_context
def status(ctx: click.Context, vm_name: str, output_json: bool) -> None:
    """Show detailed status of a VM."""
    cfg: Config = ctx.obj["config"]
    
    vm_manager = VMManager(cfg.libvirt_uri)
    
    try:
        with vm_manager:
            domain = vm_manager.get_vm_by_name(vm_name)
            metadata_mgr = MetadataManager(domain)
            
            tags = vm_manager.get_vm_tags(domain)
            metadata = metadata_mgr.get_all_metadata()
            interfaces_data = vm_manager.list_vm_interfaces(domain)
            default_ip = vm_manager.get_vm_ip(domain)
            
            if output_json:
                data = {
                    "name": vm_name,
                    "uuid": domain.UUIDString(),
                    "state": vm_manager._get_state_string(domain.state()[0]),
                    "in_use": metadata_mgr.is_in_use(),
                    "task_id": metadata_mgr.get_task_id() or "",
                    "tags": tags,
                    "metadata": metadata,
                    "networks": interfaces_data.get("networks", {}) if interfaces_data else {},
                    "interfaces": interfaces_data.get("interfaces", {}) if interfaces_data else {},
                    "default_ip": default_ip or "",
                }
                click.echo(json.dumps(data, indent=2))
                return
            
            console.print(f"[bold]VM: {vm_name}[/bold]")
            console.print(f"UUID: {domain.UUIDString()}")
            console.print(f"State: {vm_manager._get_state_string(domain.state()[0])}")
            console.print(f"In Use: {metadata_mgr.is_in_use()}")
            
            task_id = metadata_mgr.get_task_id()
            if task_id:
                console.print(f"Task ID: {task_id}")
            
            if metadata:
                console.print("\n[bold]Metadata:[/bold]")
                for key, value in metadata.items():
                    console.print(f"  {key}: {value}")
            
            if tags:
                console.print(f"\n[bold]Tags:[/bold] {', '.join(tags)}")
            
            # Show all network interfaces
            if interfaces_data:
                # Show networks (preferred)
                if interfaces_data.get("networks"):
                    console.print("\n[bold]Networks:[/bold]")
                    for network_name, ips in interfaces_data["networks"].items():
                        if ips:
                            console.print(f"  {network_name}: {', '.join(ips)}")
                        else:
                            console.print(f"  {network_name}: (no IP)")
                
                # Show interfaces (for reference)
                if interfaces_data.get("interfaces"):
                    console.print("\n[bold]Interfaces:[/bold]")
                    for iface_name, ips in interfaces_data["interfaces"].items():
                        if ips:
                            console.print(f"  {iface_name}: {', '.join(ips)}")
                        else:
                            console.print(f"  {iface_name}: (no IP)")
            
            # Also show the IP that would be used for deployment
            if default_ip:
                console.print(f"\n[bold]Default IP[/bold] (first interface): {default_ip}")
                
    except VMNotFoundException as e:
        if output_json:
            click.echo(json.dumps({"error": str(e)}, indent=2))
        else:
            console.print(f"[bold red]Error: {e}[/bold red]")
        sys.exit(1)




@main.command()
@click.option("--vm-name", required=True, help="VM name to reset")
@click.pass_context
def reset_vm(ctx: click.Context, vm_name: str) -> None:
    """Manually reset a VM (wipefs + reboot)."""
    cfg: Config = ctx.obj["config"]
    
    vm_manager = VMManager(cfg.libvirt_uri)
    reset_manager = VMResetManager()
    
    try:
        with vm_manager:
            domain = vm_manager.get_vm_by_name(vm_name)
            
            console.print(f"[yellow]Resetting VM: {vm_name}[/yellow]")
            reset_manager.reset_vm(domain)
            console.print("[green]VM reset complete[/green]")
            
            # Mark as available
            metadata_mgr = MetadataManager(domain)
            metadata_mgr.mark_available()
            
    except (VMNotFoundException, VMResetError) as e:
        console.print(f"[bold red]Error: {e}[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    main()