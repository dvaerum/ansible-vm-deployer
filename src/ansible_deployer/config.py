"""
Configuration management for ansible-deployer.
"""
from pathlib import Path
from typing import Optional, Dict, List
from pydantic import BaseModel, ConfigDict, Field

import yaml


class LibvirtConnectionConfig(BaseModel):
    """Configuration for a single libvirt connection."""

    uri: str = Field(description="Libvirt connection URI (e.g. qemu:///system, qemu+ssh://user@host/system?keyfile=/path)")
    network: Optional[str] = Field(default=None, description="Preferred libvirt network name for IP resolution on this host")


class Config(BaseModel):
    """Application configuration.

    Supports two config formats for libvirt connections:

    1. Legacy single-URI format (backward compatible)::

        libvirt_uri: "qemu:///system"

    2. Named multi-host connections::

        libvirt_connections:
          local:
            uri: "qemu:///system"
          remote:
            uri: "qemu+ssh://root@10.0.0.5/system?keyfile=/root/.ssh/id_rsa"
            network: "mgmt-net"

    When both are present, ``libvirt_connections`` takes precedence.
    """

    # Legacy single-URI field (backward compatible)
    libvirt_uri: Optional[str] = Field(default=None, description="Libvirt connection URI (legacy, use libvirt_connections instead)")

    # Multi-host connections
    libvirt_connections: Optional[Dict[str, LibvirtConnectionConfig]] = Field(
        default=None,
        description="Named libvirt connections with per-host settings",
    )

    model_config = ConfigDict(extra="allow")

    def get_connections(self) -> Dict[str, LibvirtConnectionConfig]:
        """Return the resolved connection configurations.

        Priority:
          1. ``libvirt_connections`` if present
          2. ``libvirt_uri`` wrapped as a single connection named 'default'
          3. Fallback: ``qemu:///system`` as 'default'

        Returns:
            Dict of connection name to LibvirtConnectionConfig
        """
        if self.libvirt_connections:
            return self.libvirt_connections

        uri = self.libvirt_uri or "qemu:///system"
        return {"default": LibvirtConnectionConfig(uri=uri)}

    @classmethod
    def _default_search_paths(cls, project_root: Optional[Path] = None) -> List[Path]:
        """Return the ordered list of default config file search paths.

        Search order (first match wins):
          1. ./config.yaml   (current working directory)
          2. ./config.yml
          3. <project_root>/config.yaml   (if --project-root is set)
          4. <project_root>/config.yml
          5. ~/.config/ansible-deployer/config.yaml   (XDG user config)
          6. /etc/ansible-deployer/config.yaml         (system-wide)
        """
        paths = [
            Path("config.yaml"),
            Path("config.yml"),
        ]
        if project_root is not None:
            paths.extend([
                project_root / "config.yaml",
                project_root / "config.yml",
            ])
        paths.extend([
            Path.home() / ".config" / "ansible-deployer" / "config.yaml",
            Path("/etc/ansible-deployer/config.yaml"),
        ])
        return paths

    @classmethod
    def load(
        cls,
        config_path: Optional[Path] = None,
        project_root: Optional[Path] = None,
    ) -> "Config":
        """Load configuration from file.

        If *config_path* is not given, the default search paths are tried in
        order (see ``_default_search_paths``).

        Args:
            config_path:  Explicit path to a config file (from ``--config``).
            project_root: Project root directory (from ``--project-root``),
                          used to add an extra search path.

        Returns:
            Tuple-like: (Config object, path that was loaded or None)
        """
        loaded_from: Optional[Path] = None

        if config_path is not None:
            loaded_from = config_path
        else:
            for path in cls._default_search_paths(project_root):
                if path.exists():
                    loaded_from = path
                    break

        if loaded_from is None or not loaded_from.exists():
            return cls()

        with open(loaded_from) as f:
            data = yaml.safe_load(f)

        instance = cls(**data if data else {})
        # Bypass Pydantic's __setattr__ to avoid storing this as an extra field
        # (which would leak into model_dump() and thus into save() output).
        object.__setattr__(instance, "_loaded_from", loaded_from)
        return instance

    @property
    def loaded_from(self) -> Optional[Path]:
        """Return the file path the config was loaded from, or None for defaults."""
        return getattr(self, "_loaded_from", None)

    def save(self, config_path: Path) -> None:
        """Save configuration to file.

        Args:
            config_path: Path to save config
        """
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            yaml.dump(self.model_dump(exclude_none=True), f, default_flow_style=False)
