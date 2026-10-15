"""
Configuration for slurm-mcp.

Two sources, both read once at startup:

  .env           — per-user secrets and connection details (HPC_HOST, HPC_USER, key, …)
  cluster.toml   — per-cluster description (paths, partitions, QoS, docs, script preamble)

Everything is collected into an immutable ``Settings`` object returned by ``get_settings()``.
Tests (or embedding code) can inject their own via ``set_settings()``.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv

# ── Limits ─────────────────────────────────────────────────────────────────────
MAX_FILE_SIZE_BYTES = 1 * 1024 * 1024  # files above this are previewed, never loaded
MAX_WRITE_SIZE_BYTES = 5 * 1024 * 1024  # largest file hpc_write_file will upload
MAX_FILE_PREVIEW_LINES = 100
MAX_OUTPUT_LINES = 200  # cap on directory listings, find results, queue lines

# Path components that are always blocked, wherever they appear in a path.
SENSITIVE_PATH_NAMES = frozenset({".ssh", ".gnupg", ".netrc", ".aws", ".kube", ".docker"})

BUILTIN_DOCS_DIR = Path(__file__).parent / "builtin_docs"

DEFAULT_CLUSTER_CONFIG = "cluster/cluster.toml"


class ConfigError(Exception):
    """Raised for an invalid cluster.toml."""


@dataclass(frozen=True)
class Partition:
    name: str
    description: str = ""
    max_time: str | None = None  # SLURM time format, e.g. "14-00:00:00"
    max_cpus: int | None = None  # None = not validated
    max_mem_gb: int | None = None
    max_gpus: int | None = None  # 0 = partition has no GPUs


@dataclass(frozen=True)
class Settings:
    # ── SSH ──
    host: str = ""
    user: str = ""
    port: int = 22
    key_path: str = ""
    key_passphrase: str | None = None
    password: str | None = None
    jump_host: str = ""
    jump_user: str = ""
    jump_port: int = 22
    known_hosts: str = ""
    verify_host_key: bool = True
    command_timeout: int = 30

    # ── Cluster ──
    cluster_name: str = "HPC cluster"
    cluster_description: str = ""
    login_shell: bool = False

    # ── Path policy (placeholders already expanded) ──
    write_paths: tuple[str, ...] = ()
    read_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()

    # ── SLURM ──
    partitions: tuple[Partition, ...] = ()
    default_partition: str = ""
    qos: tuple[str, ...] = ()
    account: str = ""
    preamble: tuple[str, ...] = ()
    no_qos_max_cpus: int | None = None
    no_qos_max_mem_gb: int | None = None
    no_qos_max_gpus: int | None = None

    # ── Optional features ──
    workspaces_enabled: bool = False
    workspace_max_days: int = 30
    rmd_setup: tuple[str, ...] = ()

    # ── Docs / projects ──
    docs_dir: Path | None = None
    projects: dict[str, str] = field(default_factory=dict)
    default_project: str = ""

    config_path: str = ""  # where cluster.toml was loaded from ("" = not found)

    def partition(self, name: str) -> Partition | None:
        return next((p for p in self.partitions if p.name == name), None)


# ═══════════════════════════════════════════════════════════════════════════════
# LOADING
# ═══════════════════════════════════════════════════════════════════════════════


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value and value.strip() else default
    except ValueError:
        return default


def _expand(path: str, user: str) -> str:
    """Substitute placeholders in a remote path from cluster.toml."""
    return path.replace("{user}", user)


def _str_list(table: dict[str, Any], key: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"'{key}' must be a list of strings")
    return value


def _optional_int(table: dict[str, Any], key: str) -> int | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"'{key}' must be an integer")
    return value


def parse_projects(raw: str, write_paths: tuple[str, ...]) -> dict[str, str]:
    """
    Parse HPC_PROJECTS ("alias:/abs/path,alias2:relative/dir").
    Relative paths are resolved against the first writable path.
    """
    base = next((p for p in write_paths if not any(c in p for c in "*?[")), "")
    projects: dict[str, str] = {}
    for item in raw.split(","):
        if ":" not in item:
            continue
        alias, path = (part.strip() for part in item.split(":", 1))
        if not alias or not path:
            continue
        if not path.startswith("/") and base:
            path = f"{base.rstrip('/')}/{path}"
        projects[alias] = path
    return projects


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Build Settings from environment variables and the cluster.toml they point to."""
    if env is None:
        load_dotenv(find_dotenv(usecwd=True))
        env = dict(os.environ)

    user = env.get("HPC_USER", "").strip()

    config_path = Path(env.get("HPC_CLUSTER_CONFIG") or DEFAULT_CLUSTER_CONFIG).expanduser()
    data: dict[str, Any] = {}
    if config_path.is_file():
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{config_path}: {e}") from e

    cluster = data.get("cluster", {})
    paths = data.get("paths", {})
    slurm = data.get("slurm", {})
    no_qos = slurm.get("limits_without_qos", {})
    workspaces = data.get("workspaces", {})
    rmd = data.get("rmd", {})

    if any("name" not in p for p in slurm.get("partitions", [])):
        raise ConfigError("every [[slurm.partitions]] entry needs a 'name'")
    partitions = tuple(
        Partition(
            name=p["name"],
            description=p.get("description", ""),
            max_time=p.get("max_time"),
            max_cpus=_optional_int(p, "max_cpus"),
            max_mem_gb=_optional_int(p, "max_mem_gb"),
            max_gpus=_optional_int(p, "max_gpus"),
        )
        for p in slurm.get("partitions", [])
    )
    default_partition = slurm.get("default_partition", "") or (
        partitions[0].name if partitions else ""
    )

    write_paths = tuple(_expand(p, user) for p in _str_list(paths, "write"))

    # Docs live next to cluster.toml unless overridden.
    docs_override = env.get("HPC_DOCS_DIR", "")
    if docs_override:
        docs_dir: Path | None = Path(docs_override).expanduser()
    elif config_path.is_file():
        docs_dir = config_path.parent / cluster.get("docs_dir", "docs")
    else:
        docs_dir = None

    return Settings(
        host=env.get("HPC_HOST", "").strip(),
        user=user,
        port=_env_int(env.get("HPC_PORT"), 22),
        key_path=str(Path(env["HPC_KEY_PATH"]).expanduser()) if env.get("HPC_KEY_PATH") else "",
        key_passphrase=env.get("HPC_KEY_PASSPHRASE") or None,
        password=env.get("HPC_PASSWORD") or None,
        jump_host=env.get("HPC_JUMP_HOST", "").strip(),
        jump_user=env.get("HPC_JUMP_USER", "").strip(),
        jump_port=_env_int(env.get("HPC_JUMP_PORT"), 22),
        known_hosts=str(Path(env.get("HPC_KNOWN_HOSTS") or "~/.ssh/known_hosts").expanduser()),
        verify_host_key=_env_bool(env.get("HPC_VERIFY_HOST_KEY"), True),
        command_timeout=_env_int(env.get("HPC_COMMAND_TIMEOUT"), 30),
        cluster_name=cluster.get("name", "HPC cluster"),
        cluster_description=_expand(cluster.get("description", ""), user).strip(),
        login_shell=bool(cluster.get("login_shell", False)),
        write_paths=write_paths,
        read_paths=tuple(_expand(p, user) for p in _str_list(paths, "read")),
        forbidden_paths=tuple(_expand(p, user) for p in _str_list(paths, "forbidden")),
        partitions=partitions,
        default_partition=default_partition,
        qos=tuple(_str_list(slurm, "qos")),
        account=slurm.get("account", ""),
        preamble=tuple(_str_list(slurm, "preamble")),
        no_qos_max_cpus=_optional_int(no_qos, "max_cpus"),
        no_qos_max_mem_gb=_optional_int(no_qos, "max_mem_gb"),
        no_qos_max_gpus=_optional_int(no_qos, "max_gpus"),
        workspaces_enabled=bool(workspaces.get("enabled", False)),
        workspace_max_days=int(workspaces.get("max_days", 30)),
        rmd_setup=tuple(_str_list(rmd, "setup")),
        docs_dir=docs_dir,
        projects=parse_projects(env.get("HPC_PROJECTS", ""), write_paths),
        default_project=env.get("HPC_DEFAULT_PROJECT", "").strip(),
        config_path=str(config_path) if config_path.is_file() else "",
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide Settings, loading them on first use."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def set_settings(settings: Settings | None) -> None:
    """Replace the process-wide Settings (None forces a reload on next access)."""
    global _settings
    _settings = settings
