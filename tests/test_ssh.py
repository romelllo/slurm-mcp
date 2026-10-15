"""Tests for ssh.py — connection options, auto-reconnect, response shaping."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import asyncssh
import pytest

from slurm_mcp.ssh import SSHManager, _to_str


def _run_result(stdout="", stderr="", exit_status=0) -> MagicMock:
    result = MagicMock()
    result.stdout, result.stderr, result.exit_status = stdout, stderr, exit_status
    return result


def _conn(run_result: MagicMock | None = None) -> AsyncMock:
    conn = AsyncMock()
    conn.run = AsyncMock(return_value=run_result or _run_result())
    conn.get_extra_info = MagicMock(return_value=object())  # live transport
    conn.close = MagicMock()
    return conn


class TestToStr:
    def test_variants(self) -> None:
        assert _to_str(None) == ""
        assert _to_str("x") == "x"
        assert _to_str(b"x") == "x"
        assert isinstance(_to_str(b"\xff\xfe"), str)


class TestConnect:
    async def test_requires_host_and_user(self, use_settings) -> None:
        use_settings(host="")
        with pytest.raises(ConnectionError, match="HPC_HOST"):
            await SSHManager().connect()

    async def test_key_and_known_hosts(self, use_settings, tmp_path) -> None:
        known = tmp_path / "known_hosts"
        known.write_text("")
        use_settings(key_path="/keys/id", known_hosts=str(known), port=2222)
        with patch("slurm_mcp.ssh.asyncssh.connect", AsyncMock(return_value=_conn())) as connect:
            await SSHManager().connect()
        args, kwargs = connect.call_args
        assert args == ("login.example.org",)
        assert kwargs["port"] == 2222
        assert kwargs["username"] == "alice"
        assert kwargs["client_keys"] == ["/keys/id"]
        assert kwargs["known_hosts"] == str(known)
        assert "password" not in kwargs

    async def test_missing_known_hosts_is_clear_error(self, use_settings, tmp_path) -> None:
        use_settings(known_hosts=str(tmp_path / "missing"))
        with pytest.raises(ConnectionError, match="known_hosts file not found"):
            await SSHManager().connect()

    async def test_verification_disabled(self, use_settings) -> None:
        use_settings(verify_host_key=False, password="pw")
        with patch("slurm_mcp.ssh.asyncssh.connect", AsyncMock(return_value=_conn())) as connect:
            await SSHManager().connect()
        assert connect.call_args.kwargs["known_hosts"] is None
        assert connect.call_args.kwargs["password"] == "pw"

    async def test_jump_host_tunnel(self, use_settings) -> None:
        use_settings(verify_host_key=False, jump_host="gw.example.org", jump_user="gwuser")
        jump, target = _conn(), _conn()
        with patch("slurm_mcp.ssh.asyncssh.connect", AsyncMock(side_effect=[jump, target])) as c:
            mgr = SSHManager()
            await mgr.connect()
        first, second = c.call_args_list
        assert first.args == ("gw.example.org",)
        assert first.kwargs["username"] == "gwuser"
        assert second.args == ("login.example.org",)
        assert second.kwargs["tunnel"] is jump
        assert mgr._conn is target


class TestRun:
    @pytest.fixture
    def mgr(self, use_settings) -> SSHManager:
        use_settings(verify_host_key=False)
        return SSHManager()

    async def test_success_is_trimmed(self, mgr) -> None:
        mgr._conn = _conn(_run_result(stdout="  out\n"))
        assert await mgr.run("ls") == {"stdout": "out"}

    async def test_strip_false_keeps_whitespace(self, mgr) -> None:
        mgr._conn = _conn(_run_result(stdout="  out\n"))
        assert (await mgr.run("cat f", strip=False))["stdout"] == "  out\n"

    async def test_success_with_stderr(self, mgr) -> None:
        mgr._conn = _conn(_run_result(stdout="ok", stderr="warn"))
        assert await mgr.run("x") == {"stdout": "ok", "stderr": "warn"}

    async def test_failure_shape(self, mgr) -> None:
        mgr._conn = _conn(_run_result(stderr="nope", exit_status=2))
        result = await mgr.run("x")
        assert result["success"] is False
        assert result["exit_code"] == 2

    async def test_input_forwarded(self, mgr) -> None:
        mgr._conn = _conn()
        await mgr.run("cat > f", input="data")
        assert mgr._conn.run.call_args.kwargs["input"] == "data"

    async def test_login_shell_wraps(self, use_settings) -> None:
        use_settings(verify_host_key=False, login_shell=True)
        mgr = SSHManager()
        mgr._conn = _conn()
        await mgr.run("squeue -u 'a b'")
        assert mgr._conn.run.call_args.args[0] == "bash -lc 'squeue -u '\"'\"'a b'\"'\"''"

    async def test_timeout(self, mgr) -> None:
        conn = _conn()

        async def slow(*a, **k):
            await asyncio.sleep(5)

        conn.run = slow
        mgr._conn = conn
        result = await mgr.run("sleep", timeout=0.01)  # type: ignore[arg-type]
        assert "timed out" in result["stderr"]

    async def test_reconnects_once(self, mgr) -> None:
        dead = _conn()
        dead.run = AsyncMock(side_effect=asyncssh.ConnectionLost("gone"))
        fresh = _conn(_run_result(stdout="ok"))
        mgr._conn = dead
        with patch("slurm_mcp.ssh.asyncssh.connect", AsyncMock(return_value=fresh)):
            assert await mgr.run("x") == {"stdout": "ok"}

    async def test_gives_up_after_second_failure(self, mgr) -> None:
        with patch("slurm_mcp.ssh.asyncssh.connect", AsyncMock(side_effect=OSError("refused"))):
            result = await mgr.run("x")
        assert "SSH connection error" in result["stderr"]

    async def test_auth_failure_message(self, mgr) -> None:
        err = asyncssh.PermissionDenied("denied")
        with patch("slurm_mcp.ssh.asyncssh.connect", AsyncMock(side_effect=err)):
            result = await mgr.run("x")
        assert "authentication failed for alice@login.example.org" in result["stderr"]

    async def test_close(self, mgr) -> None:
        conn = _conn()
        mgr._conn = conn
        await mgr.close()
        conn.close.assert_called_once()
        assert mgr._conn is None
