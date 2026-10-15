"""
hpc-workspace tools (ws_list / ws_allocate / ws_extend / ws_release).
Only registered when cluster.toml sets [workspaces] enabled = true.
"""

import re
import shlex

from ..config import get_settings
from ..ssh import ok, ssh_manager

_WS_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def _bad_name(name: str) -> dict | None:
    if not _WS_NAME_RE.fullmatch(name):
        return {"error": f"Invalid workspace name {name!r} (letters, digits, '.', '_', '-')."}
    return None


async def list_workspaces() -> dict:
    return await ssh_manager.run("ws_list")


async def workspace_info(name: str) -> dict:
    if bad := _bad_name(name):
        return bad
    path = await ssh_manager.run(f"ws_find {shlex.quote(name)}")
    details = await ssh_manager.run(f"ws_list | grep -A6 -F -- {shlex.quote(name)}")
    return {
        "workspace": name,
        "path": path.get("stdout") or None,
        "details": details.get("stdout") or details.get("stderr", "") or "not found",
    }


async def allocate_workspace(name: str, days: int) -> dict:
    if bad := _bad_name(name):
        return bad
    max_days = get_settings().workspace_max_days
    if not 1 <= days <= max_days:
        return {"error": f"days must be between 1 and {max_days}."}
    return await ssh_manager.run(f"ws_allocate {shlex.quote(name)} {days}")


async def extend_workspace(name: str, days: int) -> dict:
    if bad := _bad_name(name):
        return bad
    max_days = get_settings().workspace_max_days
    if not 1 <= days <= max_days:
        return {"error": f"days must be between 1 and {max_days}."}
    return await ssh_manager.run(f"ws_extend {shlex.quote(name)} {days}")


async def release_workspace(name: str, delete_data: bool = False) -> dict:
    """
    Release a workspace. Without delete_data, ws_release only hides the data —
    it still counts against quota until the cluster's cleanup runs.
    """
    if bad := _bad_name(name):
        return bad
    flag = "--delete-data " if delete_data else ""
    result = await ssh_manager.run(f"ws_release {flag}{shlex.quote(name)}")
    if ok(result) and not delete_data:
        result["warning"] = (
            "Workspace released but data NOT deleted; it still uses quota. "
            "Call again with delete_data=true to remove the data permanently."
        )
    return result
