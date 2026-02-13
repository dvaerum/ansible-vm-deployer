"""
SSH connectivity checker with retry logic.

Checks if a VM is accessible via SSH with full authentication.
Retries on connection failures but gives up on authentication failures.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional
import paramiko
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@dataclass
class SSHConfig:
    """
    SSH authentication configuration.
    
    Attributes:
        username: SSH username
        key_path: Path to SSH private key (optional)
        password: SSH password (optional)
        port: SSH port (default: 22)
    """
    username: str
    key_path: Optional[str] = None
    password: Optional[str] = None
    port: int = 22


class SSHChecker:
    """
    Checks SSH connectivity to VMs with retry logic.
    
    Performs full SSH authentication (not just TCP connect) to verify
    the VM is ready. Retries on connection refused, but gives up on
    authentication failures.
    """
    
    def __init__(
        self,
        ssh_config: SSHConfig,
        check_interval: int = 10,
        max_wait_time: Optional[int] = None
    ):
        """
        Initialize the SSH checker.
        
        Args:
            ssh_config: SSH authentication configuration
            check_interval: Seconds between retry attempts (default: 10)
            max_wait_time: Maximum seconds to wait (None = infinite)
        """
        self.ssh_config = ssh_config
        self.check_interval = check_interval
        self.max_wait_time = max_wait_time
    
    async def wait_for_ssh(
        self,
        hostname: str,
        vm_name: str,
        max_wait_time_override: Optional[float] = None
    ) -> str:
        """
        Wait for SSH to become available on a VM.
        
        Retries connection attempts until SSH succeeds or timeout is reached.
        Returns immediately on authentication failure (misconfiguration).
        
        Args:
            hostname: IP address or hostname of the VM
            vm_name: Name of the VM (for logging)
            max_wait_time_override: If provided, use this timeout instead of
                self.max_wait_time. Used when IP resolution already consumed
                part of the overall time budget.
            
        Returns:
            "success" if SSH succeeded with fresh boot confirmed
            "auth_failure" if SSH authentication failed (config issue)
            "timeout" if max_wait_time was exceeded
        """
        effective_max_wait = (
            max_wait_time_override
            if max_wait_time_override is not None
            else self.max_wait_time
        )
        start_time = datetime.now()
        attempt = 0
        
        logger.info(
            f"Waiting for SSH on {vm_name} ({hostname}), "
            f"check interval: {self.check_interval}s"
            + (f", max wait: {effective_max_wait:.0f}s" if effective_max_wait else ", no timeout")
        )
        
        while True:
            attempt += 1
            
            # Check if we've exceeded max wait time
            if effective_max_wait is not None:
                elapsed = (datetime.now() - start_time).total_seconds()
                if elapsed >= effective_max_wait:
                    logger.warning(
                        f"SSH check for {vm_name} ({hostname}) timed out after "
                        f"{elapsed:.1f}s ({attempt} attempts)"
                    )
                    return "timeout"
            
            # Attempt SSH connection
            result = await self._try_ssh_connect(hostname, vm_name, attempt)
            
            if result == "success":
                elapsed = (datetime.now() - start_time).total_seconds()
                logger.info(
                    f"SSH successful for {vm_name} ({hostname}) after "
                    f"{elapsed:.1f}s ({attempt} attempts)"
                )
                return "success"
            
            elif result == "auth_failure":
                # Authentication failed - don't retry, this is a config issue
                logger.error(
                    f"SSH authentication failed for {vm_name} ({hostname}) - "
                    "check your credentials"
                )
                return "auth_failure"
            
            # Connection failed - retry after interval
            logger.debug(
                f"SSH connection failed for {vm_name} ({hostname}), "
                f"retrying in {self.check_interval}s (attempt {attempt})"
            )
            await asyncio.sleep(self.check_interval)
    
    async def _try_ssh_connect(
        self,
        hostname: str,
        vm_name: str,
        attempt: int
    ) -> str:
        """
        Attempt a single SSH connection.
        
        Args:
            hostname: IP address or hostname
            vm_name: VM name (for logging)
            attempt: Attempt number (for logging)
            
        Returns:
            "success" if connected and authenticated
            "auth_failure" if authentication failed
            "connection_failed" if connection failed (retry-able)
        """
        # Use run_in_executor to run paramiko in a thread pool
        # (paramiko is synchronous, but we want async)
        loop = asyncio.get_event_loop()
        
        try:
            result = await loop.run_in_executor(
                None,
                self._ssh_connect_sync,
                hostname,
                vm_name,
                attempt
            )
            return result
        except Exception as e:
            logger.debug(
                f"Unexpected error during SSH attempt {attempt} to {vm_name} "
                f"({hostname}): {e}"
            )
            return "connection_failed"
    
    def _ssh_connect_sync(
        self,
        hostname: str,
        vm_name: str,
        attempt: int
    ) -> str:
        """
        Synchronous SSH connection attempt (runs in thread pool).
        
        Args:
            hostname: IP address or hostname
            vm_name: VM name (for logging)
            attempt: Attempt number (for logging)
            
        Returns:
            "success", "auth_failure", or "connection_failed"
        """
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            # Build connection kwargs
            connect_kwargs = {
                "hostname": hostname,
                "port": self.ssh_config.port,
                "username": self.ssh_config.username,
                "timeout": 10,  # TCP connection timeout
                "banner_timeout": 10,  # SSH banner timeout
                "auth_timeout": 10,  # Authentication timeout
            }
            
            # Add authentication method
            if self.ssh_config.key_path:
                connect_kwargs["key_filename"] = self.ssh_config.key_path
            elif self.ssh_config.password:
                connect_kwargs["password"] = self.ssh_config.password
            else:
                logger.error("No SSH authentication method provided")
                return "auth_failure"
            
            # Attempt connection
            client.connect(**connect_kwargs)
            
            # Connection successful - now verify the VM actually rebooted
            # by checking uptime (to avoid race condition where we connect
            # to the OLD boot before the reboot completes)
            try:
                stdin, stdout, stderr = client.exec_command("cat /proc/uptime", timeout=5)
                uptime_output = stdout.read().decode('utf-8').strip()
                uptime_seconds = float(uptime_output.split()[0])
                
                # Only consider it successful if VM booted recently (< 120 seconds)
                # This ensures we're connecting to a FRESH boot, not the old one
                if uptime_seconds < 120:
                    logger.debug(
                        f"SSH successful for {vm_name} ({hostname}), "
                        f"uptime: {uptime_seconds:.1f}s (fresh boot confirmed)"
                    )
                    client.close()
                    return "success"
                else:
                    logger.debug(
                        f"SSH connected to {vm_name} ({hostname}) but uptime is "
                        f"{uptime_seconds:.1f}s (> 120s) - VM hasn't rebooted yet, "
                        "will retry"
                    )
                    client.close()
                    return "connection_failed"
            except Exception as e:
                # If uptime check fails, treat as connection failure and retry
                logger.debug(
                    f"Failed to verify uptime for {vm_name} ({hostname}): {e}, "
                    "will retry"
                )
                client.close()
                return "connection_failed"
        
        except paramiko.AuthenticationException as e:
            # Authentication failed - this is a configuration error
            logger.debug(f"SSH auth failed for {vm_name} ({hostname}): {e}")
            client.close()
            return "auth_failure"
        
        except (
            paramiko.SSHException,
            OSError,
            TimeoutError,
            ConnectionError
        ) as e:
            # Connection failed - retry-able
            logger.debug(
                f"SSH connection failed for {vm_name} ({hostname}) "
                f"on attempt {attempt}: {e}"
            )
            client.close()
            return "connection_failed"
        
        except Exception as e:
            # Unexpected error
            logger.warning(
                f"Unexpected SSH error for {vm_name} ({hostname}): {e}",
                exc_info=True
            )
            client.close()
            return "connection_failed"
