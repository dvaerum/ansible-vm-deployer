"""
Command-line interface for vm-manager daemon.
"""
import argparse
import os
import sys
import logging
import asyncio
from pathlib import Path
from typing import List, Optional

from .daemon import run_daemon
from .ssh_checker import SSHConfig


def main():
    """Main entry point for vm-manager CLI."""
    parser = argparse.ArgumentParser(
        prog="vm-manager",
        description="Monitor VMs and remove tags after SSH becomes available",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage
  vm-manager --tag used --ssh-username root --ssh-key ~/.ssh/id_rsa

  # With boot at start
  vm-manager --tag used --ssh-username root --ssh-key ~/.ssh/id_rsa --boot-at-start

  # With max wait time
  vm-manager --tag used --ssh-username ansible --ssh-key /keys/ansible.key --max-wait-time 300

  # Check existing running VMs
  vm-manager --tag used --ssh-username root --ssh-key ~/.ssh/id_rsa --check-existing
        """
    )

    # Required arguments
    parser.add_argument(
        "--tag",
        action="append",
        required=True,
        help="VM tag to monitor (can specify multiple times)"
    )
    parser.add_argument(
        "--ssh-username",
        required=True,
        help="SSH username for connectivity checks"
    )

    # SSH authentication (at least one required)
    auth_group = parser.add_argument_group("SSH authentication (at least one required)")
    auth_group.add_argument(
        "--ssh-key",
        type=Path,
        help="Path to SSH private key"
    )
    auth_group.add_argument(
        "--ssh-password-file",
        type=Path,
        help="Path to file containing SSH password"
    )

    # Optional tag filtering
    parser.add_argument(
        "--exclude-tag",
        action="append",
        help="Exclude VMs with this tag (can specify multiple times)"
    )

    # Tag removal behavior
    parser.add_argument(
        "--mark-as-used",
        nargs="?",
        const="used",
        metavar="TAG",
        help="Tag name to remove (default: 'used' if flag provided without value)"
    )

    # Boot modes (mutually exclusive)
    boot_group = parser.add_mutually_exclusive_group()
    boot_group.add_argument(
        "--boot-at-start",
        action="store_true",
        help="Boot all matching shutdown VMs once at startup"
    )
    boot_group.add_argument(
        "--boot-always",
        action="store_true",
        help="Continuously boot matching shutdown VMs (daemon monitors and boots them)"
    )

    # Timing options
    parser.add_argument(
        "--check-interval",
        type=int,
        default=10,
        metavar="SECONDS",
        help="Interval between SSH checks in seconds (default: 10)"
    )
    parser.add_argument(
        "--max-wait-time",
        type=int,
        default=1800,
        metavar="SECONDS",
        help="Maximum time to wait for SSH in seconds (default: 1800 = 30 minutes)"
    )

    # Broken VM tagging
    parser.add_argument(
        "--broken-tag",
        default="broken",
        metavar="TAG",
        help="Tag to add to VMs that fail SSH after max-wait-time (default: 'broken'). "
             "Use --no-broken-tag to disable."
    )
    parser.add_argument(
        "--no-broken-tag",
        action="store_true",
        help="Don't add a tag to VMs that fail SSH (just stop monitoring them)"
    )

    # On-broken handler
    parser.add_argument(
        "--on-broken",
        type=Path,
        metavar="SCRIPT",
        help="Path to external script/program to run when a VM is marked broken. "
             "VM information is passed via environment variables: "
             "VM_NAME, VM_UUID, VM_IP, VM_TAGS, VM_BROKEN_TAG, VM_WAIT_TIME, LIBVIRT_URI"
    )

    # Startup behavior
    parser.add_argument(
        "--check-existing",
        action="store_true",
        help="Check existing running VMs at startup and remove tags if SSH ready"
    )

    # Libvirt connection
    parser.add_argument(
        "--libvirt-uri",
        default="qemu:///system",
        help="Libvirt connection URI (default: qemu:///system)"
    )

    # Logging
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default="info",
        help="Log level (default: info)"
    )

    args = parser.parse_args()

    # Validate authentication: at least one method required
    if not args.ssh_key and not args.ssh_password_file:
        parser.error("At least one of --ssh-key or --ssh-password-file is required")

    # Validate SSH key exists
    if args.ssh_key and not args.ssh_key.exists():
        parser.error(f"SSH key not found: {args.ssh_key}")

    # Validate password file exists
    if args.ssh_password_file and not args.ssh_password_file.exists():
        parser.error(f"Password file not found: {args.ssh_password_file}")

    # Validate on-broken script exists and is executable
    if args.on_broken:
        if not args.on_broken.exists():
            parser.error(f"On-broken script not found: {args.on_broken}")
        if not os.access(args.on_broken, os.X_OK):
            parser.error(f"On-broken script is not executable: {args.on_broken}")

    # Setup logging
    log_level = getattr(logging, args.log_level.upper())
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    logger = logging.getLogger(__name__)
    logger.info("VM Manager starting...")
    logger.info(f"Monitoring tags: {', '.join(args.tag)}")
    if args.exclude_tag:
        logger.info(f"Excluding tags: {', '.join(args.exclude_tag)}")
    logger.info(f"SSH username: {args.ssh_username}")
    if args.ssh_key:
        logger.info(f"SSH key: {args.ssh_key}")
    if args.ssh_password_file:
        logger.info(f"SSH password file: {args.ssh_password_file}")
    logger.info(f"Check interval: {args.check_interval}s")
    if args.max_wait_time:
        logger.info(f"Max wait time: {args.max_wait_time}s ({args.max_wait_time // 60} minutes)")
    else:
        logger.info("Max wait time: infinite")
    
    # Determine broken tag
    broken_tag: Optional[str] = None
    if not args.no_broken_tag:
        broken_tag = args.broken_tag
        logger.info(f"Broken tag: '{broken_tag}' (added to VMs that fail SSH after timeout)")
    else:
        logger.info("Broken tag: disabled")
    
    # Determine on-broken script
    on_broken: Optional[str] = None
    if args.on_broken:
        on_broken = str(args.on_broken)
        logger.info(f"On-broken script: {on_broken}")
    
    logger.info(f"Libvirt URI: {args.libvirt_uri}")

    # Determine tags to remove
    tags_to_remove: List[str] = []
    if args.mark_as_used is not None:
        # Flag was provided, use the value (default "used")
        tags_to_remove = [args.mark_as_used]
        logger.info(f"Will remove tag: {args.mark_as_used}")
    else:
        # Flag not provided, remove the monitored tags
        tags_to_remove = args.tag
        logger.info(f"Will remove monitored tags: {', '.join(tags_to_remove)}")
    
    # Read SSH password if provided
    ssh_password: Optional[str] = None
    if args.ssh_password_file:
        try:
            ssh_password = args.ssh_password_file.read_text().strip()
        except Exception as e:
            logger.error(f"Failed to read password file: {e}")
            return 1
    
    # Build SSH config
    ssh_config = SSHConfig(
        username=args.ssh_username,
        key_path=str(args.ssh_key) if args.ssh_key else None,
        password=ssh_password,
        port=22
    )
    
    # Run the daemon
    try:
        asyncio.run(run_daemon(
            libvirt_uri=args.libvirt_uri,
            ssh_config=ssh_config,
            monitor_tags=args.tag,
            exclude_tags=args.exclude_tag or [],
            tags_to_remove=tags_to_remove,
            check_interval=args.check_interval,
            max_wait_time=args.max_wait_time,
            check_existing=args.check_existing,
            boot_at_start=args.boot_at_start,
            boot_always=args.boot_always,
            broken_tag=broken_tag,
            on_broken=on_broken
        ))
        return 0
    except Exception as e:
        logger.error(f"Daemon failed: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
