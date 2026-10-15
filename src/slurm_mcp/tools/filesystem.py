"""
Safe filesystem operations on the cluster.

Rules enforced here:
  - No deletion, moving, or permission changes — there is simply no tool for them
  - Writes only under the configured write paths
  - Never pull files > MAX_FILE_SIZE_BYTES into context (head/tail preview instead)
  - Every path goes through paths.guard() (policy + symlink resolution)
"""

import re
import shlex

from ..config import (
    MAX_FILE_PREVIEW_LINES,
    MAX_FILE_SIZE_BYTES,
    MAX_OUTPUT_LINES,
    MAX_WRITE_SIZE_BYTES,
    get_settings,
)
from ..paths import guard
from ..ssh import ok, ssh_manager

_SIZE_FILTER_RE = re.compile(r"^[+-]?\d+[kMG]?$")


def _q(value: str) -> str:
    return shlex.quote(value)


def _truncate(result: dict, limit: int = MAX_OUTPUT_LINES, hint: str = "") -> dict:
    lines = result.get("stdout", "").splitlines()
    if len(lines) > limit:
        result["stdout"] = "\n".join(lines[:limit])
        result["truncated"] = True
        result["note"] = f"Output truncated to {limit} lines. {hint}".strip()
    return result


async def list_directory(path: str, pattern: str = "") -> dict:
    """List directory contents — never fetches file data."""
    error, path = await guard(path)
    if error:
        return {"error": error}

    if pattern:
        cmd = f"find {_q(path)} -maxdepth 1 -name {_q(pattern)} | sort | head -n {MAX_OUTPUT_LINES + 1}"
    else:
        cmd = f"ls -lh --time-style=long-iso -- {_q(path)}"
    return _truncate(await ssh_manager.run(cmd), hint="Use a pattern to filter.")


async def get_file_info(path: str) -> dict:
    """Metadata (size, type, permissions, mtime) without fetching content."""
    error, path = await guard(path)
    if error:
        return {"error": error}
    return await ssh_manager.run(f"stat -- {_q(path)} && file -- {_q(path)}")


async def read_file(path: str) -> dict:
    """
    Read a text file into context if it's under MAX_FILE_SIZE_BYTES.
    Larger files return a head/tail preview; binary files are refused.
    """
    error, path = await guard(path)
    if error:
        return {"error": error}

    meta = await ssh_manager.run(
        f"stat -c %s -- {_q(path)} && file -b --mime-encoding -- {_q(path)}"
    )
    if not ok(meta):
        return meta
    size_line, _, encoding = meta["stdout"].partition("\n")
    try:
        size_bytes = int(size_line.strip())
    except ValueError:
        return {"error": f"Could not determine size of {path}: {meta['stdout']}"}

    if encoding.strip() == "binary":
        return {
            "error": f"{path} is a binary file ({size_bytes} bytes) and cannot be read as text.",
            "advice": "Use hpc_get_file_info for metadata, or hpc_head_file for compressed text.",
        }

    if size_bytes > MAX_FILE_SIZE_BYTES:
        n = MAX_FILE_PREVIEW_LINES
        preview = await ssh_manager.run(
            f"head -n {n} -- {_q(path)}; echo '... [middle of file omitted] ...'; "
            f"tail -n {n} -- {_q(path)}"
        )
        return {
            "warning": "large_file",
            "path": path,
            "size_mb": round(size_bytes / 1024 / 1024, 2),
            "limit_mb": round(MAX_FILE_SIZE_BYTES / 1024 / 1024, 2),
            "preview": preview.get("stdout", ""),
            "advice": (
                "File is too large to load into context. Use hpc_search_in_file to find "
                "specific sections, or hpc_tail_file / hpc_head_file."
            ),
        }

    result = await ssh_manager.run(f"cat -- {_q(path)}", strip=False)
    if ok(result):
        result["path"] = path
        result["size_bytes"] = size_bytes
    return result


async def head_file(path: str, lines: int = 50) -> dict:
    """First N lines of a file; gzip/bgzip files are decompressed on the fly."""
    error, path = await guard(path)
    if error:
        return {"error": error}
    if not 1 <= lines <= 1000:
        return {"error": "lines must be between 1 and 1000."}

    result = await ssh_manager.run(f"zcat -f -- {_q(path)} | head -n {lines}")
    result["path"] = path
    return result


async def tail_file(path: str, lines: int = 50) -> dict:
    """Last N lines of a file — safe for large and still-growing files (job logs)."""
    error, path = await guard(path)
    if error:
        return {"error": error}
    if not 1 <= lines <= 1000:
        return {"error": "lines must be between 1 and 1000."}

    result = await ssh_manager.run(f"tail -n {lines} -- {_q(path)}")
    result["path"] = path
    return result


async def search_in_file(path: str, pattern: str, context_lines: int = 3) -> dict:
    """grep for a pattern — streams the file, so safe for any size."""
    error, path = await guard(path)
    if error:
        return {"error": error}
    if not 0 <= context_lines <= 20:
        return {"error": "context_lines must be between 0 and 20."}

    result = await ssh_manager.run(
        f"grep -n -C {context_lines} -e {_q(pattern)} -- {_q(path)} | head -n {MAX_OUTPUT_LINES}"
    )
    result["path"] = path
    result["pattern"] = pattern
    if not result.get("stdout") and ok(result):
        result["note"] = "No matches."
    return result


async def find_files(
    base_path: str,
    name_pattern: str = "*",
    file_type: str = "f",
    max_depth: int = 3,
    size_filter: str = "",
) -> dict:
    """Find files matching criteria — returns paths only."""
    error, base_path = await guard(base_path)
    if error:
        return {"error": error}
    if file_type not in {"f", "d", "l"}:
        return {"error": "file_type must be one of 'f', 'd', 'l'."}
    if not 1 <= max_depth <= 10:
        return {"error": "max_depth must be between 1 and 10."}
    if size_filter and not _SIZE_FILTER_RE.match(size_filter):
        return {"error": "size_filter must look like '+1G', '-100M' or '500k'."}

    cmd = f"find {_q(base_path)} -maxdepth {max_depth} -type {file_type} -name {_q(name_pattern)}"
    if size_filter:
        cmd += f" -size {size_filter}"
    cmd += f" 2>/dev/null | head -n {MAX_OUTPUT_LINES + 1}"

    result = _truncate(await ssh_manager.run(cmd), hint="Narrow the pattern or depth.")
    result["base_path"] = base_path
    return result


async def write_file(path: str, content: str) -> dict:
    """Create or overwrite a file under the configured write paths."""
    error, path = await guard(path, for_write=True)
    if error:
        return {"error": error}
    if len(content.encode()) > MAX_WRITE_SIZE_BYTES:
        return {"error": f"Content exceeds {MAX_WRITE_SIZE_BYTES // 1024 // 1024} MB write limit."}

    # Content travels over stdin — no quoting issues and no command-line length limit.
    cmd = f'mkdir -p -- "$(dirname -- {_q(path)})" && cat > {_q(path)}'
    result = await ssh_manager.run(cmd, input=content, timeout=60)
    if ok(result):
        result["message"] = f"File written to {path}"
    return result


async def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> dict:
    """
    Replace an exact string in a file under the write paths.
    Fails if ``old_string`` occurs more than once unless ``replace_all`` is set.
    """
    error, path = await guard(path, for_write=True)
    if error:
        return {"error": error}
    if not old_string:
        return {"error": "old_string must not be empty."}

    current = await ssh_manager.run(f"cat -- {_q(path)}", strip=False)
    if not ok(current):
        return current
    content = current["stdout"]

    count = content.count(old_string)
    if count == 0:
        return {"error": "old_string not found in file. No changes made."}
    if count > 1 and not replace_all:
        return {
            "error": (
                f"Found {count} occurrences of old_string. Provide more surrounding context "
                "to make the match unique, or set replace_all=true."
            )
        }

    new_content = content.replace(old_string, new_string, -1 if replace_all else 1)
    result = await ssh_manager.run(f"cat > {_q(path)}", input=new_content, timeout=60)
    if ok(result):
        replaced = count if replace_all else 1
        result["message"] = f"Edited {path} ({replaced} occurrence{'s' if replaced != 1 else ''})."
    return result


async def storage_usage() -> dict:
    """Free space on the configured filesystems, plus quota if the cluster has it."""
    s = get_settings()
    roots = [p for p in s.write_paths + s.read_paths if not any(c in p for c in "*?[")]
    if not roots:
        return {"error": "No paths configured in cluster.toml [paths]."}
    quoted = " ".join(_q(r) for r in roots)
    df = await ssh_manager.run(f"df -h {quoted} 2>/dev/null | awk 'NR==1 || !seen[$0]++'")
    quota = await ssh_manager.run("quota -s 2>/dev/null")
    return {
        "df": df.get("stdout", "") or df.get("stderr", ""),
        "quota": quota.get("stdout", "") or "quota command not available or no quotas set",
    }
