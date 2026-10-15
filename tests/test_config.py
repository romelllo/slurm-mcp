"""Tests for config.py — env + cluster.toml loading."""

from __future__ import annotations

import pytest

from slurm_mcp.config import ConfigError, load_settings, parse_projects

TOML = """
[cluster]
name = "Demo"
description = "Home is /home/{user}"
login_shell = true

[paths]
write = ["/scratch/{user}/"]
read = ["/data/shared/"]
forbidden = ["/etc/"]

[slurm]
default_partition = "gpu"
qos = ["high"]
preamble = ["module load foo"]

[slurm.limits_without_qos]
max_cpus = 32

[[slurm.partitions]]
name = "cpu"
max_time = "1-00:00:00"
max_cpus = 64

[[slurm.partitions]]
name = "gpu"
max_gpus = 4

[workspaces]
enabled = true
max_days = 60
"""


@pytest.fixture
def cluster_toml(tmp_path):
    path = tmp_path / "cluster.toml"
    path.write_text(TOML)
    (tmp_path / "docs").mkdir()
    return path


class TestLoadSettings:
    def test_full_profile(self, cluster_toml) -> None:
        s = load_settings(
            {
                "HPC_HOST": "login.example.org",
                "HPC_USER": "bob",
                "HPC_PORT": "2222",
                "HPC_CLUSTER_CONFIG": str(cluster_toml),
                "HPC_JUMP_HOST": "gw.example.org",
            }
        )
        assert s.host == "login.example.org"
        assert s.port == 2222
        assert s.jump_host == "gw.example.org"
        assert s.cluster_name == "Demo"
        assert s.cluster_description == "Home is /home/bob"
        assert s.login_shell is True
        assert s.write_paths == ("/scratch/bob/",)
        assert s.read_paths == ("/data/shared/",)
        assert s.forbidden_paths == ("/etc/",)
        assert [p.name for p in s.partitions] == ["cpu", "gpu"]
        assert s.partition("cpu").max_cpus == 64  # type: ignore[union-attr]
        assert s.partition("gpu").max_cpus is None  # type: ignore[union-attr]
        assert s.default_partition == "gpu"
        assert s.qos == ("high",)
        assert s.preamble == ("module load foo",)
        assert s.no_qos_max_cpus == 32
        assert s.workspaces_enabled is True
        assert s.workspace_max_days == 60
        assert s.docs_dir == cluster_toml.parent / "docs"
        assert s.config_path == str(cluster_toml)

    def test_missing_profile_gives_empty_policy(self, tmp_path) -> None:
        s = load_settings({"HPC_USER": "bob", "HPC_CLUSTER_CONFIG": str(tmp_path / "nope.toml")})
        assert s.write_paths == ()
        assert s.read_paths == ()
        assert s.partitions == ()
        assert s.docs_dir is None
        assert s.config_path == ""

    def test_default_partition_falls_back_to_first(self, tmp_path) -> None:
        path = tmp_path / "c.toml"
        path.write_text(
            '[[slurm.partitions]]\nname = "short"\n[[slurm.partitions]]\nname = "long"\n'
        )
        s = load_settings({"HPC_CLUSTER_CONFIG": str(path)})
        assert s.default_partition == "short"

    def test_invalid_toml_raises(self, tmp_path) -> None:
        path = tmp_path / "c.toml"
        path.write_text("[paths\n")
        with pytest.raises(ConfigError):
            load_settings({"HPC_CLUSTER_CONFIG": str(path)})

    def test_wrong_type_raises(self, tmp_path) -> None:
        path = tmp_path / "c.toml"
        path.write_text('[paths]\nwrite = "/scratch"\n')
        with pytest.raises(ConfigError):
            load_settings({"HPC_CLUSTER_CONFIG": str(path)})

    def test_host_key_verification_flags(self, tmp_path) -> None:
        s = load_settings({"HPC_VERIFY_HOST_KEY": "false", "HPC_KNOWN_HOSTS": "/tmp/kh"})
        assert s.verify_host_key is False
        assert s.known_hosts == "/tmp/kh"
        assert load_settings({}).verify_host_key is True

    def test_secrets_empty_means_none(self) -> None:
        s = load_settings({"HPC_PASSWORD": "", "HPC_KEY_PASSPHRASE": ""})
        assert s.password is None
        assert s.key_passphrase is None

    def test_docs_dir_override(self, cluster_toml, tmp_path) -> None:
        s = load_settings({"HPC_CLUSTER_CONFIG": str(cluster_toml), "HPC_DOCS_DIR": "/opt/docs"})
        assert str(s.docs_dir) == "/opt/docs"


class TestParseProjects:
    def test_absolute_and_relative(self) -> None:
        projects = parse_projects("a:/abs/path, b:rel/dir", ("/scratch/bob/",))
        assert projects == {"a": "/abs/path", "b": "/scratch/bob/rel/dir"}

    def test_relative_skips_glob_roots(self) -> None:
        projects = parse_projects("b:rel", ("/ws/bob-*", "/scratch/bob"))
        assert projects == {"b": "/scratch/bob/rel"}

    def test_malformed_entries_ignored(self) -> None:
        assert parse_projects("noalias,:x,y:,", ()) == {}
