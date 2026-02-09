"""
VM Manager - Daemon for managing VM tags based on SSH availability.

This daemon monitors libvirt VM lifecycle events and removes tags from VMs
after they become accessible via SSH following a boot/reboot.
"""

__version__ = "0.1.0"
