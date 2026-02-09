"""
Libvirt connection management with user-friendly error handling.
"""
import libvirt
from typing import Optional


# Suppress noisy libvirt error messages printed to stderr.
# libvirt's C library writes "metadata not found" etc. to stderr before
# raising the Python exception. Our code handles these exceptions correctly,
# but the stderr output is confusing to users.
def _libvirt_error_handler(ctx, err):
    """Silently ignore libvirt errors (handled via Python exceptions)."""
    pass


libvirt.registerErrorHandler(_libvirt_error_handler, None)


class LibvirtConnection:
    """Manages libvirt connection with context manager support."""

    def __init__(self, uri: str = "qemu:///system"):
        """Initialize libvirt connection manager.
        
        Args:
            uri: Libvirt connection URI
        """
        self.uri = uri
        self.conn: Optional[libvirt.virConnect] = None

    def connect(self) -> None:
        """Establish connection to libvirt.
        
        Raises:
            RuntimeError: If connection fails with user-friendly error message
        """
        try:
            self.conn = libvirt.open(self.uri)
            if self.conn is None:
                raise RuntimeError(f"Failed to connect to libvirt at {self.uri}")
        except libvirt.libvirtError as e:
            error_msg = str(e)
            
            # Check for polkit/authentication errors
            if "polkit" in error_msg.lower() or "authentication" in error_msg.lower():
                raise RuntimeError(
                    "Libvirt authentication failed. This tool requires system-level access to libvirt.\n"
                    "\n"
                    "Solutions:\n"
                    "  1. Run with sudo: sudo <command> ...\n"
                    "  2. Add your user to the 'libvirt' group: sudo usermod -aG libvirt $USER\n"
                    "     (Then log out and back in for group changes to take effect)\n"
                    "  3. Configure polkit to allow your user access to libvirt\n"
                    "\n"
                    f"Original error: {error_msg}"
                ) from e
            
            # Check for connection refused errors
            elif "refused" in error_msg.lower() or "failed to connect" in error_msg.lower():
                raise RuntimeError(
                    f"Failed to connect to libvirt at {self.uri}\n"
                    "\n"
                    "Possible causes:\n"
                    "  - libvirtd service is not running: sudo systemctl start libvirtd\n"
                    "  - Incorrect URI (check --libvirt-uri or LIBVIRT_DEFAULT_URI)\n"
                    "  - Firewall blocking connection\n"
                    "\n"
                    f"Original error: {error_msg}"
                ) from e
            
            # Generic libvirt error
            else:
                raise RuntimeError(
                    f"Failed to connect to libvirt at {self.uri}\n"
                    f"Error: {error_msg}"
                ) from e

    def disconnect(self) -> None:
        """Close libvirt connection."""
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "LibvirtConnection":
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type: type, exc_val: Exception, exc_tb: object) -> None:
        """Context manager exit."""
        self.disconnect()

    def get_connection(self) -> libvirt.virConnect:
        """Get the underlying libvirt connection.
        
        Returns:
            libvirt connection object
            
        Raises:
            RuntimeError: If not connected
        """
        if self.conn is None:
            raise RuntimeError("Not connected to libvirt")
        return self.conn
