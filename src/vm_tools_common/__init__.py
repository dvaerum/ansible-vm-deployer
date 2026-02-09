"""
Shared library for VM management tools.

This library provides common functionality for interacting with libvirt VMs,
including tag operations, IP resolution, and connection management.
"""

from .exceptions import VMNotFoundException, NoAvailableVMException
from .vm_operations import (
    get_vm_tags,
    add_vm_tag,
    remove_vm_tag,
    get_vm_ip,
    get_network_to_interface_mapping,
    list_vm_interfaces,
    get_state_string,
)
from .libvirt_connection import LibvirtConnection
from .tag_filters import vm_matches_tags

__all__ = [
    "VMNotFoundException",
    "NoAvailableVMException",
    "get_vm_tags",
    "add_vm_tag",
    "remove_vm_tag",
    "get_vm_ip",
    "get_network_to_interface_mapping",
    "list_vm_interfaces",
    "get_state_string",
    "LibvirtConnection",
    "vm_matches_tags",
]
