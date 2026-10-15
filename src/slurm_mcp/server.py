"""
slurm-mcp server.
Exposes a SLURM cluster (reached over SSH) as MCP tools over stdio.
"""

import asyncio
import contextlib
import inspect
import json
import shlex
import signal
import sys
from collections.abc import Awaitable, Callable
from typing import Any, cast

from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.types import TextContent, Tool, ToolAnnotations

from . import __version__
from .config import MAX_FILE_SIZE_BYTES, get_settings
from .ssh import ok, ssh_manager
from .tools import docs, filesystem, scripts, slurm, workspaces

# Tools that change state on the cluster. Clients should ask before running these.
MUTATING_TOOLS = frozenset(
    {
        "hpc_write_file",
        "hpc_edit_file",
        "hpc_submit_job",
        "hpc_cancel_job",
        "hpc_hold_job",
        "hpc_release_job",
        "hpc_allocate_workspace",
        "hpc_extend_workspace",
        "hpc_release_workspace",
    }
)
_DESTRUCTIVE_TOOLS = frozenset({"hpc_cancel_job", "hpc_release_workspace", "hpc_write_file"})


def build_instructions() -> str:
    """Server instructions sent to the client at initialisation."""
    s = get_settings()
    lines = [
        f"This server gives access to {s.cluster_name}, a SLURM cluster, over SSH "
        f"(login node {s.host or '<HPC_HOST not set>'}, user {s.user or '<HPC_USER not set>'}).",
    ]
    if s.cluster_description:
        lines.append(s.cluster_description)
    lines += [
        "",
        "Use the hpc_* tools for anything on the cluster: remote files, SLURM jobs, queues, "
        "partitions, job logs. Never use local filesystem or shell tools for cluster paths.",
        f"Writable: {', '.join(s.write_paths) or 'none configured'}.",
        f"Read-only: {', '.join(s.read_paths) or 'none'}.",
    ]
    if s.partitions:
        parts = ", ".join(
            p.name + (" (default)" if p.name == s.default_partition else "") for p in s.partitions
        )
        lines.append(f"Partitions: {parts}.")
    if s.qos:
        lines.append(f"QoS values: {', '.join(s.qos)}.")
    lines += [
        "",
        "Workflow for running work: hpc_generate_sbatch → show the script to the user → "
        "hpc_write_file → hpc_submit_job → hpc_queue / hpc_tail_file on the log.",
        "Heavy computation must go through SLURM; the tools only run light commands on the "
        "login node.",
        "Read hpc_list_docs / hpc_read_doc for cluster rules before suggesting resources.",
        "Always confirm with the user before calling a tool that changes state: "
        + ", ".join(sorted(MUTATING_TOOLS))
        + ".",
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> Tool:
    return Tool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": properties or {}, "required": required or []},
        annotations=ToolAnnotations(
            read_only_hint=name not in MUTATING_TOOLS,
            destructive_hint=name in _DESTRUCTIVE_TOOLS,
            open_world_hint=False,
        ),
    )


_PATH = {"type": "string", "description": "Absolute path on the cluster"}
_JOB_ID = {"type": "string", "description": "SLURM job ID, e.g. '123456' or '123456_7'"}


def build_tools() -> list[Tool]:
    s = get_settings()
    partition_schema: dict[str, Any] = {
        "type": "string",
        "description": f"Partition (default: {s.default_partition or 'cluster default'})",
    }
    if s.partitions:
        partition_schema["enum"] = [p.name for p in s.partitions]
    qos_schema: dict[str, Any] = {"type": "string", "description": "QoS (optional)"}
    if s.qos:
        qos_schema["enum"] = list(s.qos)
    size_mb = MAX_FILE_SIZE_BYTES // 1024 // 1024

    tools = [
        # ── Documentation ──────────────────────────────────────────────────────
        _tool("hpc_list_docs", "List the cluster documentation (site rules + SLURM basics)."),
        _tool(
            "hpc_read_doc",
            "Read one documentation file. Partial names work, e.g. 'slurm', 'storage'.",
            {"filename": {"type": "string"}},
            ["filename"],
        ),
        _tool(
            "hpc_search_docs",
            "Search all cluster documentation for a keyword or phrase.",
            {"query": {"type": "string"}},
            ["query"],
        ),
        # ── Filesystem ─────────────────────────────────────────────────────────
        _tool(
            "hpc_list_directory",
            "List a directory on the cluster (ls -lh). Does not fetch file content.",
            {
                "path": _PATH,
                "pattern": {"type": "string", "description": "Optional glob, e.g. '*.bam'"},
            },
            ["path"],
        ),
        _tool(
            "hpc_get_file_info",
            "File metadata (size, type, permissions, mtime) without reading content.",
            {"path": _PATH},
            ["path"],
        ),
        _tool(
            "hpc_read_file",
            f"Read a text file into context (≤{size_mb} MB). Larger files return a "
            "head/tail preview; binary files are refused.",
            {"path": _PATH},
            ["path"],
        ),
        _tool(
            "hpc_head_file",
            "First N lines of a file. Transparently decompresses .gz files (FASTQ, VCF, BED, …).",
            {"path": _PATH, "lines": {"type": "integer", "default": 50, "maximum": 1000}},
            ["path"],
        ),
        _tool(
            "hpc_tail_file",
            "Last N lines of a file. Ideal for checking live SLURM job logs.",
            {"path": _PATH, "lines": {"type": "integer", "default": 50, "maximum": 1000}},
            ["path"],
        ),
        _tool(
            "hpc_search_in_file",
            "grep a file for a pattern (basic regex). Streams, so safe for huge files.",
            {
                "path": _PATH,
                "pattern": {"type": "string"},
                "context_lines": {"type": "integer", "default": 3},
            },
            ["path", "pattern"],
        ),
        _tool(
            "hpc_find_files",
            "Find files by name/type/size under a directory. Returns paths only.",
            {
                "base_path": _PATH,
                "name_pattern": {"type": "string", "default": "*"},
                "file_type": {"type": "string", "enum": ["f", "d", "l"], "default": "f"},
                "max_depth": {"type": "integer", "default": 3},
                "size_filter": {"type": "string", "description": "e.g. '+1G', '-100M'"},
            },
            ["base_path"],
        ),
        _tool(
            "hpc_storage_usage",
            "Free space (df) on the configured filesystems and the user's quota.",
        ),
        _tool(
            "hpc_write_file",
            "Create or overwrite a file in a writable location "
            f"({', '.join(s.write_paths) or 'none configured'}). Parent dirs are created.",
            {"path": _PATH, "content": {"type": "string"}},
            ["path", "content"],
        ),
        _tool(
            "hpc_edit_file",
            "Replace an exact string in a file in a writable location. Fails if old_string "
            "is not unique unless replace_all=true.",
            {
                "path": _PATH,
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean", "default": False},
            },
            ["path", "old_string", "new_string"],
        ),
        _tool(
            "hpc_resolve_project",
            "Resolve a project alias to its directory. With no alias, lists projects "
            "and the readable/writable roots.",
            {"alias": {"type": "string", "default": ""}},
        ),
        # ── SLURM ──────────────────────────────────────────────────────────────
        _tool(
            "hpc_queue",
            "Show the SLURM queue (your jobs by default).",
            {"user_only": {"type": "boolean", "default": True}},
        ),
        _tool(
            "hpc_cluster_info",
            "Partitions and node availability (sinfo). per_node=true shows CPUs, memory "
            "and GPUs for every node.",
            {"per_node": {"type": "boolean", "default": False}},
        ),
        _tool(
            "hpc_job_details",
            "Full details of one job (scontrol + sacct).",
            {"job_id": _JOB_ID},
            ["job_id"],
        ),
        _tool(
            "hpc_job_efficiency",
            "CPU and memory efficiency of a finished job (seff/sacct). Use it to right-size "
            "future requests.",
            {"job_id": _JOB_ID},
            ["job_id"],
        ),
        _tool(
            "hpc_job_history",
            "Your jobs from the last N days (sacct), optionally filtered by state.",
            {
                "days": {"type": "integer", "default": 7},
                "state": {"type": "string", "description": "e.g. 'FAILED,TIMEOUT,OUT_OF_MEMORY'"},
            },
        ),
        _tool("hpc_list_reservations", "List SLURM reservations."),
        _tool(
            "hpc_submit_job",
            "Submit an existing batch script with sbatch (runs from the script's directory). "
            "The script must be in a writable location.",
            {"script_path": _PATH},
            ["script_path"],
        ),
        _tool(
            "hpc_cancel_job",
            "Cancel one of your own jobs (scancel).",
            {"job_id": _JOB_ID},
            ["job_id"],
        ),
        _tool(
            "hpc_hold_job", "Hold one of your own pending jobs.", {"job_id": _JOB_ID}, ["job_id"]
        ),
        _tool(
            "hpc_release_job", "Release one of your own held jobs.", {"job_id": _JOB_ID}, ["job_id"]
        ),
        # ── Script generation ──────────────────────────────────────────────────
        _tool(
            "hpc_generate_sbatch",
            "Generate an sbatch script, validated against the cluster's partition/QoS limits. "
            "Returns text only — save with hpc_write_file.",
            {
                "job_name": {"type": "string"},
                "cpus": {"type": "integer"},
                "memory_gb": {"type": "integer"},
                "walltime": {"type": "string", "description": "HH:MM:SS or D-HH:MM:SS"},
                "commands": {"type": "array", "items": {"type": "string"}},
                "output_dir": {"type": "string", "description": "Writable dir for .out/.err logs"},
                "partition": partition_schema,
                "qos": qos_schema,
                "gpu": {"type": "string", "description": "GPU count '1' or type:count 'a100:1'"},
                "nodelist": {"type": "string", "description": "Specific node(s), e.g. 'node01'"},
                "account": {"type": "string"},
                "array": {"type": "string", "description": "Array spec, e.g. '1-10%4'"},
                "email": {"type": "string"},
                "mail_type": {"type": "string", "default": "END,FAIL"},
                "extra_directives": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional sbatch options, e.g. ['--constraint=avx2']",
                },
            },
            ["job_name", "cpus", "memory_gb", "walltime", "commands", "output_dir"],
        ),
        _tool(
            "hpc_generate_rmd",
            "Generate an R Markdown notebook template that references data paths on the cluster.",
            {
                "title": {"type": "string"},
                "author": {"type": "string"},
                "data_paths": {"type": "array", "items": {"type": "string"}},
                "analysis_type": {
                    "type": "string",
                    "enum": ["QC", "DESeq2", "survival", "scRNA", "general"],
                },
                "output_dir": {"type": "string"},
                "r_packages": {"type": "array", "items": {"type": "string"}},
            },
            ["title", "author", "data_paths", "analysis_type", "output_dir"],
        ),
        _tool(
            "hpc_generate_rmd_render_job",
            "Generate an sbatch script that renders an R Markdown file on a compute node.",
            {
                "rmd_path": _PATH,
                "output_dir": {"type": "string"},
                "cpus": {"type": "integer", "default": 4},
                "memory_gb": {"type": "integer", "default": 16},
                "walltime": {"type": "string", "default": "04:00:00"},
                "partition": partition_schema,
                "email": {"type": "string"},
            },
            ["rmd_path", "output_dir"],
        ),
    ]

    if s.workspaces_enabled:
        ws_name = {"type": "string", "description": "Workspace name"}
        days = {"type": "integer", "description": f"Days (max {s.workspace_max_days})"}
        tools += [
            _tool("hpc_list_workspaces", "List your hpc-workspace workspaces (ws_list)."),
            _tool(
                "hpc_workspace_info",
                "Path and expiry of one workspace.",
                {"name": ws_name},
                ["name"],
            ),
            _tool(
                "hpc_allocate_workspace",
                "Create a new workspace (ws_allocate).",
                {"name": ws_name, "days": days},
                ["name", "days"],
            ),
            _tool(
                "hpc_extend_workspace",
                "Extend a workspace's lifetime (ws_extend).",
                {"name": ws_name, "days": days},
                ["name", "days"],
            ),
            _tool(
                "hpc_release_workspace",
                "Release a workspace (ws_release). Without delete_data=true the data is only "
                "hidden and still uses quota.",
                {"name": ws_name, "delete_data": {"type": "boolean", "default": False}},
                ["name"],
            ),
        ]
    return tools


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL DISPATCHER
# ═══════════════════════════════════════════════════════════════════════════════

Handler = Callable[[dict[str, Any]], Awaitable[dict] | dict]

_HANDLERS: dict[str, Handler] = {
    # ── Docs ──
    "hpc_list_docs": lambda a: docs.list_docs(),
    "hpc_read_doc": lambda a: docs.read_doc(a["filename"]),
    "hpc_search_docs": lambda a: docs.search_docs(a["query"]),
    # ── Filesystem ──
    "hpc_list_directory": lambda a: filesystem.list_directory(a["path"], a.get("pattern", "")),
    "hpc_get_file_info": lambda a: filesystem.get_file_info(a["path"]),
    "hpc_read_file": lambda a: filesystem.read_file(a["path"]),
    "hpc_head_file": lambda a: filesystem.head_file(a["path"], a.get("lines", 50)),
    "hpc_tail_file": lambda a: filesystem.tail_file(a["path"], a.get("lines", 50)),
    "hpc_search_in_file": lambda a: filesystem.search_in_file(
        a["path"], a["pattern"], a.get("context_lines", 3)
    ),
    "hpc_find_files": lambda a: filesystem.find_files(
        a["base_path"],
        a.get("name_pattern", "*"),
        a.get("file_type", "f"),
        a.get("max_depth", 3),
        a.get("size_filter", ""),
    ),
    "hpc_storage_usage": lambda a: filesystem.storage_usage(),
    "hpc_write_file": lambda a: filesystem.write_file(a["path"], a["content"]),
    "hpc_edit_file": lambda a: filesystem.edit_file(
        a["path"], a["old_string"], a["new_string"], a.get("replace_all", False)
    ),
    "hpc_resolve_project": lambda a: slurm.resolve_project(a.get("alias", "")),
    # ── SLURM ──
    "hpc_queue": lambda a: slurm.get_queue(a.get("user_only", True)),
    "hpc_cluster_info": lambda a: slurm.get_cluster_info(a.get("per_node", False)),
    "hpc_job_details": lambda a: slurm.get_job_details(a["job_id"]),
    "hpc_job_efficiency": lambda a: slurm.get_job_efficiency(a["job_id"]),
    "hpc_job_history": lambda a: slurm.get_job_history(a.get("days", 7), a.get("state", "")),
    "hpc_list_reservations": lambda a: slurm.list_reservations(),
    "hpc_submit_job": lambda a: slurm.submit_job(a["script_path"]),
    "hpc_cancel_job": lambda a: slurm.cancel_job(a["job_id"]),
    "hpc_hold_job": lambda a: slurm.hold_job(a["job_id"]),
    "hpc_release_job": lambda a: slurm.release_job(a["job_id"]),
    # ── Script generation ──
    "hpc_generate_sbatch": lambda a: scripts.generate_sbatch_script(**a),
    "hpc_generate_rmd": lambda a: scripts.generate_rmarkdown_notebook(**a),
    "hpc_generate_rmd_render_job": lambda a: scripts.generate_r_render_script(**a),
    # ── Workspaces ──
    "hpc_list_workspaces": lambda a: workspaces.list_workspaces(),
    "hpc_workspace_info": lambda a: workspaces.workspace_info(a["name"]),
    "hpc_allocate_workspace": lambda a: workspaces.allocate_workspace(a["name"], a["days"]),
    "hpc_extend_workspace": lambda a: workspaces.extend_workspace(a["name"], a["days"]),
    "hpc_release_workspace": lambda a: workspaces.release_workspace(
        a["name"], a.get("delete_data", False)
    ),
}

_WORKSPACE_TOOLS = frozenset(n for n in _HANDLERS if "workspace" in n)


async def dispatch(name: str, args: dict[str, Any]) -> dict:
    """Run a tool. Never raises — failures come back as {"error": ...}."""
    handler = _HANDLERS.get(name)
    if handler is None or (name in _WORKSPACE_TOOLS and not get_settings().workspaces_enabled):
        return {"error": f"Unknown tool: {name}"}
    try:
        result = handler(args)
        if inspect.isawaitable(result):
            result = await result
        return cast(dict, result)
    except KeyError as e:
        return {"error": f"Missing required argument: {e.args[0]}"}
    except TypeError as e:
        return {"error": f"Invalid arguments for {name}: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════════════════════════════════════════════
# MCP SERVER
# ═══════════════════════════════════════════════════════════════════════════════


async def _on_list_tools(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=build_tools())


async def _on_call_tool(
    ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    result = await dispatch(params.name, dict(params.arguments or {}))
    return types.CallToolResult(
        content=[
            TextContent(
                type="text", text=json.dumps(result, indent=2, ensure_ascii=False, default=str)
            )
        ],
        is_error="error" in result or result.get("success") is False,
    )


def create_server() -> Server:
    return Server(
        "hpc",
        version=__version__,
        instructions=build_instructions(),
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════


async def _main() -> None:
    from mcp.server.stdio import stdio_server

    app = create_server()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with stdio_server() as (read_stream, write_stream):
        serve_task = asyncio.create_task(
            app.run(read_stream, write_stream, app.create_initialization_options())
        )
        stop_task = asyncio.create_task(stop.wait())
        # Exit when the client closes stdin (serve_task ends) or on SIGINT/SIGTERM.
        await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in (serve_task, stop_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    await ssh_manager.close()


async def check() -> int:
    """`slurm-mcp --check`: print the effective config and test the connection."""
    s = get_settings()
    jump = f" via {s.jump_user or s.user}@{s.jump_host}:{s.jump_port}" if s.jump_host else ""
    verify = f"on ({s.known_hosts})" if s.verify_host_key else "OFF"
    print(f"Cluster profile : {s.config_path or 'NOT FOUND — copy cluster.example/ to cluster/'}")
    print(f"Cluster name    : {s.cluster_name}")
    print(f"SSH target      : {s.user or '<HPC_USER?>'}@{s.host or '<HPC_HOST?>'}:{s.port}{jump}")
    print(f"SSH key         : {s.key_path or '(default keys / agent)'}")
    print(f"Host key check  : {verify}")
    print(f"Writable paths  : {', '.join(s.write_paths) or '(none)'}")
    print(f"Readable paths  : {', '.join(s.read_paths) or '(none)'}")
    print(f"Partitions      : {', '.join(p.name for p in s.partitions) or '(not configured)'}")
    print(f"Docs directory  : {s.docs_dir or '(none)'}")
    print()

    print("Connecting ... ", end="", flush=True)
    result = await ssh_manager.run("hostname && (sinfo --version || echo 'sinfo not found')")
    if not ok(result):
        print(f"FAILED\n  {result.get('stderr', '')}")
        await ssh_manager.close()
        return 1
    hostname, _, slurm_version = result["stdout"].partition("\n")
    print(f"OK — logged in to {hostname}, {slurm_version or 'SLURM version unknown'}")

    status = 0
    for path in s.write_paths + s.read_paths:
        if any(c in path for c in "*?["):
            continue
        probe = await ssh_manager.run(
            f"test -d {shlex.quote(path)} && echo ok; test -w {shlex.quote(path)} && echo w"
        )
        flags = probe.get("stdout", "").split()
        writable = path in s.write_paths
        if "ok" not in flags:
            print(f"  ✗ {path} does not exist")
            status = 1
        elif writable and "w" not in flags:
            print(f"  ✗ {path} exists but is not writable by {s.user}")
            status = 1
        else:
            print(f"  ✓ {path}{' (writable)' if writable else ''}")

    await ssh_manager.close()
    print("\nAll good." if status == 0 else "\nFix the items marked ✗ in cluster.toml.")
    return status


def main() -> None:
    if "--check" in sys.argv[1:]:
        sys.exit(asyncio.run(check()))
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(_main())


if __name__ == "__main__":
    main()
