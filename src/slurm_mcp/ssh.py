"""
SSH connection manager.
Keeps one persistent connection to the login node (optionally through a jump host)
and reconnects automatically if it drops.
"""

import asyncio
import shlex
from pathlib import Path
from typing import Any

import asyncssh

from .config import get_settings


def _to_str(value: bytes | str | None) -> str:
    """Normalise asyncssh stdout/stderr to a plain string."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _error(message: str, exit_code: int = -1) -> dict:
    return {"stdout": "", "stderr": message, "exit_code": exit_code, "success": False}


class SSHManager:
    """Persistent SSH connection with auto-reconnect."""

    def __init__(self) -> None:
        self._conn: asyncssh.SSHClientConnection | None = None
        self._tunnel: asyncssh.SSHClientConnection | None = None
        self._lock = asyncio.Lock()

    def _auth_options(self) -> dict[str, Any]:
        """Options shared by the jump-host and login-node connections."""
        s = get_settings()
        options: dict[str, Any] = {"keepalive_interval": 60}
        if s.key_path:
            options["client_keys"] = [s.key_path]
            if s.key_passphrase:
                options["passphrase"] = s.key_passphrase
        if s.password:
            # Answers password / keyboard-interactive prompts, e.g. servers that accept the
            # key but still require a second factor ("partial success" in `ssh -v`).
            options["password"] = s.password
        if s.verify_host_key:
            if not Path(s.known_hosts).is_file():
                raise ConnectionError(
                    f"known_hosts file not found: {s.known_hosts}. Connect once with plain "
                    f"`ssh` to record the host key, or set HPC_VERIFY_HOST_KEY=false."
                )
            options["known_hosts"] = s.known_hosts
        else:
            options["known_hosts"] = None
        return options

    async def connect(self) -> None:
        """Establish the SSH connection to the login node."""
        s = get_settings()
        if not s.host or not s.user:
            raise ConnectionError("HPC_HOST and HPC_USER must be set (see .env.example).")

        options = self._auth_options()
        if s.jump_host:
            self._tunnel = await asyncssh.connect(
                s.jump_host, port=s.jump_port, username=s.jump_user or s.user, **options
            )
            options["tunnel"] = self._tunnel

        self._conn = await asyncssh.connect(s.host, port=s.port, username=s.user, **options)

    def _is_connection_alive(self) -> bool:
        """
        Safely check if connection is still usable.
        Avoids calling is_closing() which was removed in newer asyncssh.
        """
        if self._conn is None:
            return False
        try:
            transport = self._conn.get_extra_info("connection")
            return transport is not None
        except Exception:
            return False

    async def get_connection(self) -> asyncssh.SSHClientConnection:
        """Get active connection, reconnecting if needed."""
        async with self._lock:
            if not self._is_connection_alive():
                await self._close_quietly()
                await self.connect()
        assert self._conn is not None, "connect() must set self._conn"
        return self._conn

    async def run(
        self,
        command: str,
        timeout: int | None = None,
        input: str | None = None,
        strip: bool = True,
    ) -> dict:
        """
        Execute a command on the login node.

        On success returns ``{"stdout": ...}`` (plus ``stderr`` if non-empty).
        On failure returns the full dict with ``success: False``.
        ``input`` is fed to the command's stdin — use it for file uploads, which keeps
        large payloads off the command line. ``strip=False`` returns stdout verbatim.
        """
        s = get_settings()
        timeout = timeout or s.command_timeout
        if s.login_shell:
            command = f"bash -lc {shlex.quote(command)}"

        for attempt in range(2):
            try:
                conn = await self.get_connection()
                result = await asyncio.wait_for(
                    conn.run(command, check=False, input=input),
                    timeout=timeout,
                )
                stdout = _to_str(result.stdout)
                stdout = stdout.strip() if strip else stdout
                stderr = _to_str(result.stderr).strip()
                if result.exit_status == 0:
                    # On success return only the output — keeps LLM context small.
                    r: dict = {"stdout": stdout}
                    if stderr:
                        r["stderr"] = stderr
                    return r
                return {
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": result.exit_status,
                    "success": False,
                }

            except TimeoutError:
                return _error(f"Command timed out after {timeout}s: {command}")

            except asyncssh.PermissionDenied as e:
                return _error(f"SSH authentication failed for {s.user}@{s.host}: {e}")

            except asyncssh.HostKeyNotVerifiable as e:
                return _error(
                    f"Host key for {s.host} could not be verified against {s.known_hosts}: {e}"
                )

            except (asyncssh.DisconnectError, asyncssh.ConnectionLost, OSError) as e:
                await self._close_quietly()
                if attempt == 0:
                    continue  # connection dropped — reconnect and retry once
                return _error(f"SSH connection error: {e}")

            except Exception as e:
                return _error(f"Unexpected SSH error: {type(e).__name__}: {e}")

        return _error("Internal error: retry loop exhausted")

    async def _close_quietly(self) -> None:
        for conn in (self._conn, self._tunnel):
            if conn is None:
                continue
            try:
                conn.close()
                await asyncio.wait_for(conn.wait_closed(), timeout=5)
            except Exception:
                pass
        self._conn = None
        self._tunnel = None

    async def close(self) -> None:
        """Close SSH connection gracefully."""
        await self._close_quietly()


def ok(result: dict) -> bool:
    """True if an ``ssh_manager.run()`` result represents success."""
    return result.get("success", True)


# Singleton instance shared across all tools
ssh_manager = SSHManager()
