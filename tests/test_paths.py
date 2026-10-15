"""Tests for paths.py — access policy and symlink resolution."""

from __future__ import annotations

import pytest

from slurm_mcp.paths import check_path, guard, is_under
from slurm_mcp.ssh import ssh_manager


class TestIsUnder:
    @pytest.mark.parametrize(
        ("path", "root", "expected"),
        [
            ("/scratch/alice/x", "/scratch/alice/", True),
            ("/scratch/alice", "/scratch/alice/", True),
            ("/scratch/alicex/y", "/scratch/alice/", False),  # no prefix confusion
            ("/scratch", "/scratch/alice/", False),
            ("/ws/alice-proj/f.txt", "/ws/alice-*", True),
            ("/ws/bob-proj/f.txt", "/ws/alice-*", False),
            ("/anything", "/", True),
        ],
    )
    def test_cases(self, path, root, expected) -> None:
        assert is_under(path, root) is expected


class TestCheckPath:
    def test_relative_path_denied(self) -> None:
        assert "absolute" in (check_path("scratch/alice/x") or "")

    def test_write_in_scratch_allowed(self) -> None:
        assert check_path("/scratch/alice/job.sh", for_write=True) is None

    def test_write_in_glob_workspace_allowed(self) -> None:
        assert check_path("/ws/alice-rnaseq/job.sh", for_write=True) is None

    def test_write_in_read_only_denied(self) -> None:
        assert "Write access denied" in (check_path("/home/alice/x", for_write=True) or "")

    def test_read_in_read_only_allowed(self) -> None:
        assert check_path("/shared/data/genome.fa") is None

    def test_read_outside_denied(self) -> None:
        assert "Read access denied" in (check_path("/etc/passwd") or "")

    def test_forbidden_inside_readable_denied(self) -> None:
        assert "forbidden" in (check_path("/shared/data/private/x") or "")

    def test_dotdot_escape_denied(self) -> None:
        assert check_path("/scratch/alice/../bob/secret") is not None

    @pytest.mark.parametrize("name", [".ssh", ".gnupg", ".aws"])
    def test_credential_dirs_blocked_everywhere(self, name) -> None:
        assert "credential" in (check_path(f"/home/alice/{name}/id_rsa") or "")
        assert check_path(f"/scratch/alice/{name}", for_write=True) is not None

    def test_no_paths_configured(self, use_settings) -> None:
        use_settings(write_paths=(), read_paths=())
        assert "none configured" in (check_path("/scratch/alice/x") or "")


class TestGuard:
    async def test_plain_path_resolves(self, fake_ssh) -> None:
        error, resolved = await guard("/scratch/alice/x.txt")
        assert error is None
        assert resolved == "/scratch/alice/x.txt"

    async def test_symlink_escape_denied(self, fake_ssh) -> None:
        fake_ssh.symlinks["/scratch/alice/link"] = "/etc/shadow"
        error, _ = await guard("/scratch/alice/link")
        assert error is not None
        assert "resolves to /etc/shadow" in error

    async def test_symlink_to_read_only_denied_for_write(self, fake_ssh) -> None:
        fake_ssh.symlinks["/scratch/alice/ref"] = "/shared/data/ref"
        assert (await guard("/scratch/alice/ref"))[0] is None
        assert (await guard("/scratch/alice/ref", for_write=True))[0] is not None

    async def test_lexical_failure_skips_ssh(self, fake_ssh) -> None:
        error, _ = await guard("/etc/passwd")
        assert error is not None
        assert fake_ssh.calls == []

    async def test_realpath_failure_is_error(self, monkeypatch) -> None:
        async def failing(command, **kwargs):
            return {"stdout": "", "stderr": "boom", "success": False}

        monkeypatch.setattr(ssh_manager, "run", failing)
        error, _ = await guard("/scratch/alice/x")
        assert error is not None and "boom" in error
