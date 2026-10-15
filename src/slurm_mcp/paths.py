"""
Path access policy.

A path is checked twice: lexically (after normalising ``..``) and again after the
server resolves symlinks with ``realpath``, so a symlink inside a writable directory
cannot be used to reach a forbidden location.

Rules, in order:
  1. Path must be absolute.
  2. Any component in SENSITIVE_PATH_NAMES (.ssh, .gnupg, …) → denied.
  3. Under a forbidden path → denied.
  4. Write: must be under a write path.  Read: under a write path or a read path.

Configured paths may contain glob characters in a component, e.g. ``/scratch/ws/{user}-*``.
"""

import posixpath
import shlex
from fnmatch import fnmatchcase

from .config import SENSITIVE_PATH_NAMES, get_settings
from .ssh import ok, ssh_manager


def _components(path: str) -> list[str]:
    return [c for c in path.split("/") if c]


def is_under(path: str, root: str) -> bool:
    """True if ``path`` equals or lies inside ``root`` (root components may be globs)."""
    path_parts, root_parts = _components(path), _components(root)
    if len(path_parts) < len(root_parts):
        return False
    return all(fnmatchcase(p, r) for p, r in zip(path_parts, root_parts, strict=False))


def check_path(path: str, for_write: bool = False) -> str | None:
    """Lexical policy check. Returns an error message, or None if allowed."""
    s = get_settings()
    if not path.startswith("/"):
        return f"Path must be absolute: {path}"
    norm = posixpath.normpath(path)

    if SENSITIVE_PATH_NAMES.intersection(_components(norm)):
        return f"Access to {norm} is blocked (credential directory)."
    for forbidden in s.forbidden_paths:
        if is_under(norm, forbidden):
            return f"Path {norm} is in a forbidden location ({forbidden})."

    if for_write:
        if any(is_under(norm, root) for root in s.write_paths):
            return None
        allowed = ", ".join(s.write_paths) or "(none configured — set [paths].write)"
        return f"Write access denied for {norm}. Writable locations: {allowed}"

    readable = s.write_paths + s.read_paths
    if any(is_under(norm, root) for root in readable):
        return None
    allowed = ", ".join(readable) or "(none configured — set [paths] in cluster.toml)"
    return f"Read access denied for {norm}. Readable locations: {allowed}"


async def guard(path: str, for_write: bool = False) -> tuple[str | None, str]:
    """
    Full policy check including symlink resolution on the server.
    Returns ``(error, resolved_path)``; use ``resolved_path`` for the actual operation.
    """
    error = check_path(path, for_write)
    if error:
        return error, path

    result = await ssh_manager.run(f"realpath -m -- {shlex.quote(path)}")
    if not ok(result):
        return f"Could not resolve path {path}: {result.get('stderr', '')}", path
    resolved = result["stdout"].strip() or posixpath.normpath(path)

    if resolved != posixpath.normpath(path):
        error = check_path(resolved, for_write)
        if error:
            return f"{path} resolves to {resolved}. {error}", resolved
    return None, resolved
