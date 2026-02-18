"""
VM Manager for libvirt operations with tag-based selection.

This module extends the shared vm_tools_common library with deployment-specific
logic like VM allocation, metadata management, and task claiming.

Supports connecting to multiple libvirt hosts simultaneously for cross-host
VM allocation.
"""
import logging
import libvirt
from typing import List, Optional, Dict, Tuple

from vm_tools_common import (
    VMNotFoundException,
    NoAvailableVMException,
    get_vm_tags,
    add_vm_tag as vm_ops_add_tag,
    remove_vm_tag as vm_ops_remove_tag,
    get_vm_ip,
    get_network_to_interface_mapping,
    list_vm_interfaces,
    get_state_string,
    vm_matches_tags,
)
from .config import LibvirtConnectionConfig
from .metadata_manager import MetadataManager

logger = logging.getLogger(__name__)

# Tag that vm-manager adds to VMs with SSH timeout. Always excluded from
# allocation to prevent deploying to broken VMs.
_BROKEN_TAG = "broken"


class VMManager:
    """Manages libvirt VMs across one or more hosts with tag-based selection.

    Accepts either a single URI (legacy) or a dict of named connection configs
    for multi-host operation.  When multiple hosts are configured, allocation
    searches them in config order and picks the first matching VM.

    Unreachable hosts are logged as warnings and skipped so that the remaining
    hosts can still serve VMs.
    """

    def __init__(
        self,
        connections: Optional[Dict[str, LibvirtConnectionConfig]] = None,
        *,
        uri: Optional[str] = None,
    ):
        """Initialize VM manager.

        Provide *either* ``connections`` (multi-host) or ``uri`` (single-host).
        If neither is given the default ``qemu:///system`` is used.

        Args:
            connections: Named connection configurations (from Config.get_connections())
            uri:         Legacy single-URI shorthand.  Ignored when *connections*
                         is provided.
        """
        if connections is not None:
            if not isinstance(connections, dict):
                raise TypeError(
                    f"connections must be a dict of LibvirtConnectionConfig, "
                    f"got {type(connections).__name__}. "
                    f"Use VMManager(uri='...') for single-host setup."
                )
            self._connection_configs = connections
        elif uri is not None:
            self._connection_configs = {
                "default": LibvirtConnectionConfig(uri=uri),
            }
        else:
            self._connection_configs = {
                "default": LibvirtConnectionConfig(uri="qemu:///system"),
            }

        # Map of connection name -> open virConnect (populated by connect())
        self._connections: Dict[str, libvirt.virConnect] = {}

        # Map of connection name -> LibvirtConnectionConfig for connected hosts
        # (same keys as _connections, kept in sync)
        self._connected_configs: Dict[str, LibvirtConnectionConfig] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open connections to all configured libvirt hosts.

        Unreachable hosts are logged as warnings and skipped.

        Raises:
            RuntimeError: If *no* host could be reached at all.
        """
        self._connections.clear()
        self._connected_configs.clear()

        for name, cfg in self._connection_configs.items():
            try:
                conn = libvirt.open(cfg.uri)
                if conn is None:
                    logger.warning("Failed to connect to libvirt host '%s' at %s (returned None)", name, cfg.uri)
                    continue
                self._connections[name] = conn
                self._connected_configs[name] = cfg
                logger.debug("Connected to libvirt host '%s' at %s", name, cfg.uri)
            except libvirt.libvirtError as e:
                error_msg = str(e)
                # For single-host setups, give the detailed error messages
                if len(self._connection_configs) == 1:
                    self._raise_connection_error(cfg.uri, error_msg, e)
                else:
                    logger.warning(
                        "Could not connect to libvirt host '%s' at %s: %s (skipping)",
                        name, cfg.uri, error_msg,
                    )

        if not self._connections:
            uris = ", ".join(cfg.uri for cfg in self._connection_configs.values())
            raise RuntimeError(
                f"Failed to connect to any libvirt host.\n"
                f"Configured URIs: {uris}"
            )

    def disconnect(self) -> None:
        """Close all open libvirt connections."""
        for name, conn in self._connections.items():
            try:
                conn.close()
            except Exception:
                pass
        self._connections.clear()
        self._connected_configs.clear()

    def __enter__(self) -> "VMManager":
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type: type, exc_val: Exception, exc_tb: object) -> None:
        """Context manager exit."""
        self.disconnect()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_connections(self) -> None:
        """Raise if no connections are open."""
        if not self._connections:
            raise RuntimeError("Not connected to any libvirt host")

    def _iter_domains(self) -> List[Tuple[str, libvirt.virDomain]]:
        """Iterate over all domains across all connected hosts.

        Returns a flat list of (connection_name, domain) tuples, with hosts
        in config order.
        """
        result: List[Tuple[str, libvirt.virDomain]] = []
        for name in self._connection_configs:
            conn = self._connections.get(name)
            if conn is None:
                continue
            try:
                for domain in conn.listAllDomains():
                    result.append((name, domain))
            except libvirt.libvirtError as e:
                logger.warning("Error listing VMs on host '%s': %s (skipping)", name, e)
        return result

    def _get_host_network(self, connection_name: str) -> Optional[str]:
        """Return the per-host preferred network for a connection, if configured."""
        cfg = self._connected_configs.get(connection_name)
        return cfg.network if cfg else None

    @staticmethod
    def _raise_connection_error(uri: str, error_msg: str, original: Exception) -> None:
        """Raise a user-friendly RuntimeError for a connection failure."""
        if "polkit" in error_msg.lower() or "authentication" in error_msg.lower():
            raise RuntimeError(
                "Libvirt authentication failed. This tool requires system-level access to libvirt.\n"
                "\n"
                "Solutions:\n"
                "  1. Run with sudo: sudo ansible-deployer ...\n"
                "  2. Add your user to the 'libvirt' group: sudo usermod -aG libvirt $USER\n"
                "     (Then log out and back in for group changes to take effect)\n"
                "  3. Configure polkit to allow your user access to libvirt\n"
                "\n"
                f"Original error: {error_msg}"
            ) from original
        elif "refused" in error_msg.lower() or "failed to connect" in error_msg.lower():
            raise RuntimeError(
                f"Failed to connect to libvirt at {uri}\n"
                "\n"
                "Possible causes:\n"
                "  - libvirtd service is not running: sudo systemctl start libvirtd\n"
                "  - Incorrect URI (check --config or LIBVIRT_DEFAULT_URI)\n"
                "  - Firewall blocking connection\n"
                "\n"
                f"Original error: {error_msg}"
            ) from original
        else:
            raise RuntimeError(
                f"Failed to connect to libvirt at {uri}\n"
                f"Error: {error_msg}"
            ) from original

    # ------------------------------------------------------------------
    # Public API — listing & lookup
    # ------------------------------------------------------------------

    def list_vms(self) -> List[Dict[str, str]]:
        """List all VMs across all connected hosts with their basic information.

        Returns:
            List of VM information dictionaries.  Each dict includes a
            ``host`` key with the connection name.
        """
        self._require_connections()

        vms = []
        for conn_name, domain in self._iter_domains():
            metadata_mgr = MetadataManager(domain)
            tags = get_vm_tags(domain)
            vms.append({
                "name": domain.name(),
                "uuid": domain.UUIDString(),
                "state": get_state_string(domain.state()[0]),
                "tags": tags,
                "in_use": metadata_mgr.is_in_use(),
                "task_id": metadata_mgr.get_task_id() or "",
                "host": conn_name,
            })
        return vms

    def get_vm_by_name(self, name: str) -> libvirt.virDomain:
        """Get VM by name, searching all connected hosts.

        Args:
            name: VM name

        Returns:
            libvirt domain object

        Raises:
            VMNotFoundException: If VM not found on any host
        """
        self._require_connections()

        for conn_name, conn in self._connections.items():
            try:
                return conn.lookupByName(name)
            except libvirt.libvirtError:
                continue

        raise VMNotFoundException(f"VM '{name}' not found on any connected host")

    # ------------------------------------------------------------------
    # Public API — tag operations (use domain.connect() for the conn)
    # ------------------------------------------------------------------

    def get_vm_tags(self, domain: libvirt.virDomain) -> List[str]:
        """Get tags for a VM from its XML description.

        Args:
            domain: libvirt domain object

        Returns:
            List of tags
        """
        return get_vm_tags(domain)

    def add_vm_tag(self, domain: libvirt.virDomain, tag: str) -> None:
        """Add a tag to VM's description.

        Args:
            domain: libvirt domain object
            tag: Tag to add
        """
        conn = domain.connect()
        vm_ops_add_tag(conn, domain, tag)

    def remove_vm_tag(self, domain: libvirt.virDomain, tag: str) -> None:
        """Remove a tag from VM's description.

        Args:
            domain: libvirt domain object
            tag: Tag to remove
        """
        conn = domain.connect()
        vm_ops_remove_tag(conn, domain, tag)

    # ------------------------------------------------------------------
    # Public API — VM finding & allocation
    # ------------------------------------------------------------------

    def find_available_vm_by_tags(
        self, tags: List[str], exclude_tags: Optional[List[str]] = None,
    ) -> Optional[libvirt.virDomain]:
        """Find an available VM matching any of the given tags.

        Searches all connected hosts in config order.

        Args:
            tags: List of tags to match (VM must have at least one)
            exclude_tags: List of tags to exclude (VM must have none of these).
                          The 'broken' tag is always excluded automatically.

        Returns:
            Available VM domain or None
        """
        self._require_connections()

        exclude_tags = list(exclude_tags) if exclude_tags else []
        if _BROKEN_TAG not in exclude_tags:
            exclude_tags.append(_BROKEN_TAG)

        for _conn_name, domain in self._iter_domains():
            state = domain.state()[0]
            if state != libvirt.VIR_DOMAIN_RUNNING:
                continue

            metadata_mgr = MetadataManager(domain)
            if metadata_mgr.is_in_use():
                continue

            vm_tags = get_vm_tags(domain)
            if vm_matches_tags(vm_tags, tags, exclude_tags):
                return domain

        return None

    def find_available_vms_by_tags(
        self, tags: List[str], count: int,
        exclude_tags: Optional[List[str]] = None,
    ) -> List[libvirt.virDomain]:
        """Find multiple available VMs matching any of the given tags.

        Note: This method only finds VMs without claiming them. Use
        allocate_vms() for race-condition-safe allocation.

        Searches all connected hosts in config order.

        Args:
            tags: List of tags to match (VM must have at least one)
            count: Number of VMs to find
            exclude_tags: List of tags to exclude (VM must have none of these).
                          The 'broken' tag is always excluded automatically.

        Returns:
            List of available VM domains (may be less than count)
        """
        self._require_connections()

        exclude_tags = list(exclude_tags) if exclude_tags else []
        if _BROKEN_TAG not in exclude_tags:
            exclude_tags.append(_BROKEN_TAG)
        available_vms = []

        for _conn_name, domain in self._iter_domains():
            if len(available_vms) >= count:
                break

            state = domain.state()[0]
            if state != libvirt.VIR_DOMAIN_RUNNING:
                continue

            metadata_mgr = MetadataManager(domain)
            if metadata_mgr.is_in_use():
                continue

            vm_tags = get_vm_tags(domain)
            if vm_matches_tags(vm_tags, tags, exclude_tags):
                available_vms.append(domain)

        return available_vms

    def allocate_vms(
        self, tags: List[str], count: int, task_id: str,
        exclude_tags: Optional[List[str]] = None,
    ) -> List[libvirt.virDomain]:
        """Find and atomically claim VMs, preventing race conditions.

        Searches all connected hosts in config order.  For each candidate VM,
        this method attempts to claim it by writing the task_id to metadata,
        then re-reads the metadata to verify ownership.

        Args:
            tags: List of tags to match (VM must have at least one)
            count: Number of VMs to allocate
            task_id: Unique task identifier used to claim ownership
            exclude_tags: List of tags to exclude (VM must have none of these).
                          The 'broken' tag is always excluded automatically.

        Returns:
            List of successfully claimed VM domains (may be less than count)
        """
        self._require_connections()

        exclude_tags = list(exclude_tags) if exclude_tags else []
        if _BROKEN_TAG not in exclude_tags:
            exclude_tags.append(_BROKEN_TAG)
        claimed_vms = []

        for _conn_name, domain in self._iter_domains():
            if len(claimed_vms) >= count:
                break

            state = domain.state()[0]
            if state != libvirt.VIR_DOMAIN_RUNNING:
                continue

            metadata_mgr = MetadataManager(domain)
            if metadata_mgr.is_in_use():
                continue

            vm_tags = get_vm_tags(domain)
            if not vm_matches_tags(vm_tags, tags, exclude_tags):
                continue

            if metadata_mgr.try_claim(task_id):
                claimed_vms.append(domain)

        return claimed_vms

    # ------------------------------------------------------------------
    # Public API — VM info & operations
    # ------------------------------------------------------------------

    def get_network_to_interface_mapping(self, domain: libvirt.virDomain) -> Dict[str, str]:
        """Get mapping of libvirt network names to interface names.

        Args:
            domain: libvirt domain object

        Returns:
            Dictionary mapping network names to interface names
        """
        return get_network_to_interface_mapping(domain)

    def get_vm_ip(self, domain: libvirt.virDomain, network: Optional[str] = None) -> Optional[str]:
        """Get IP address of a VM.

        Args:
            domain: libvirt domain object
            network: Optional libvirt network name to resolve IP on.
                     If None and the domain's host has a per-host network
                     configured, that network is used.

        Returns:
            IP address or None
        """
        if network is None:
            # Try to find the per-host network for this domain's connection
            network = self._get_network_for_domain(domain)
        return get_vm_ip(domain, network)

    def list_vm_interfaces(self, domain: libvirt.virDomain) -> Dict[str, Dict[str, List[str]]]:
        """List all network interfaces and their IPs for a VM.

        Args:
            domain: libvirt domain object

        Returns:
            Dictionary with 'networks' and 'interfaces' keys
        """
        return list_vm_interfaces(domain)

    def _get_state_string(self, state: int) -> str:
        """Convert libvirt state constant to string.

        Args:
            state: libvirt state constant

        Returns:
            State string
        """
        return get_state_string(state)

    def _get_network_for_domain(self, domain: libvirt.virDomain) -> Optional[str]:
        """Look up the per-host network for the host a domain belongs to.

        Returns None if no per-host network is configured or the domain's
        connection cannot be matched.
        """
        try:
            dom_conn = domain.connect()
        except Exception:
            return None
        for name, conn in self._connections.items():
            if conn is dom_conn:
                return self._get_host_network(name)
        return None
