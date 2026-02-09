"""
Shared exceptions for VM management tools.
"""


class VMNotFoundException(Exception):
    """Raised when VM is not found."""
    pass


class NoAvailableVMException(Exception):
    """Raised when no available VM with matching tags is found."""
    pass
