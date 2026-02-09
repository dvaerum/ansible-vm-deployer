"""
Configuration management for ansible-deployer.
"""
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

import yaml


class Config(BaseModel):
    """Application configuration."""

    # Libvirt settings
    libvirt_uri: str = Field(default="qemu:///system", description="Libvirt connection URI")
    
    class Config:
        """Pydantic config."""
        extra = "allow"

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "Config":
        """Load configuration from file.
        
        Args:
            config_path: Path to config file (YAML)
            
        Returns:
            Config object
        """
        if config_path is None:
            # Try default locations
            for path in [
                Path("config.yaml"),
                Path("config.yml"),
                Path("/etc/ansible-deployer/config.yaml"),
            ]:
                if path.exists():
                    config_path = path
                    break

        if config_path is None or not config_path.exists():
            # Return default config
            return cls()

        with open(config_path) as f:
            data = yaml.safe_load(f)

        return cls(**data if data else {})

    def save(self, config_path: Path) -> None:
        """Save configuration to file.
        
        Args:
            config_path: Path to save config
        """
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            yaml.dump(self.model_dump(), f, default_flow_style=False)