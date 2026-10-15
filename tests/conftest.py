"""
Shared fixtures: a test cluster profile and a fake SSH runner.
No real cluster connection is ever made.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Iterator
from dataclasses import replace

import pytest

from slurm_mcp import config
from slurm_mcp.config import Partition, Settings
from slurm_mcp.ssh import ssh_manager

TEST_SETTINGS = Settings(
    host="login.example.org",
    user="alice",
    cluster_name="Test Cluster",
    write_paths=("/scratch/alice/", "/ws/alice-*"),
    read_paths=("/home/alice/", "/shared/data/"),
    forbidden_paths=("/shared/data/private/",),
    partitions=(
        Partition(name="cpu", max_time="7-00:00:00", max_cpus=64, max_mem_gb=500, max_gpus=0),
        Partition(name="gpu", max_time="2-00:00:00", max_cpus=16, max_mem_gb=128, max_gpus=2),
    ),
    default_partition="cpu",
    qos=("normal", "high"),
    no_qos_max_cpus=32,
    no_qos_max_mem_gb=256,
)


@pytest.fixture(autouse=True)
def settings() -> Iterator[Settings]:
    config.set_settings(TEST_SETTINGS)
    yield TEST_SETTINGS
    config.set_settings(None)


@pytest.fixture
def use_settings() -> Callable[..., Settings]:
    """Override individual Settings fields for one test."""

    def _apply(**changes) -> Settings:
        new = replace(TEST_SETTINGS, **changes)
        config.set_settings(new)
        return new

    return _apply


class FakeSSH:
    """
    Stand-in for ssh_manager.run. Records every call; answers from ``responses``
    (first matching command prefix wins). ``realpath -m -- X`` echoes X unless
    overridden in ``symlinks``.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: list[tuple[str, dict]] = []
        self.symlinks: dict[str, str] = {}

    def on(self, prefix: str, **result) -> None:
        self.responses.append((prefix, result or {"stdout": ""}))

    @property
    def commands(self) -> list[str]:
        return [c["command"] for c in self.calls if not c["command"].startswith("realpath")]

    async def run(self, command: str, timeout=None, input=None, strip=True) -> dict:
        self.calls.append({"command": command, "input": input, "strip": strip})
        if command.startswith("realpath -m -- "):
            path = shlex.split(command)[-1]
            return {"stdout": self.symlinks.get(path, path)}
        for prefix, result in self.responses:
            if command.startswith(prefix):
                return dict(result)
        return {"stdout": ""}


@pytest.fixture
def fake_ssh(monkeypatch) -> FakeSSH:
    fake = FakeSSH()
    monkeypatch.setattr(ssh_manager, "run", fake.run)
    return fake
