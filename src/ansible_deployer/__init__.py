__version__ = "0.1.0"

__all__ = ["VMManager", "AnsibleExecutor", "MetadataManager"]

# Lazy imports to avoid pulling in heavy dependencies (pydantic, click, rich)
# when only a submodule is needed.  This matters for the vm-manager package
# which imports ansible_deployer.metadata_manager but doesn't need the rest.

def __getattr__(name: str):
    if name == "VMManager":
        from .vm_manager import VMManager
        return VMManager
    if name == "AnsibleExecutor":
        from .ansible_executor import AnsibleExecutor
        return AnsibleExecutor
    if name == "MetadataManager":
        from .metadata_manager import MetadataManager
        return MetadataManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
