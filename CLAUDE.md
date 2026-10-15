# CLAUDE.md

Guidance for Claude Code when working on this repository.

## What this is

Python 3.11+ MCP server (stdio) that exposes a SLURM cluster, reached over SSH, as `hpc_*`
tools. It is cluster-agnostic: site details come from `cluster/cluster.toml` + `cluster/docs/`
(gitignored; template in `cluster.example/`), credentials from `.env`.

Entry point: `slurm-mcp` → `slurm_mcp.server:main` (`--check` = config + connectivity test).

## Commands

```bash
uv sync
uv run pytest                               # all tests; SSH is always faked
uv run ruff check src/ tests/ && uv run ruff format src/ tests/
uv run ty check src/
```

## Architecture

- `config.py`: `load_settings()` builds an immutable `Settings` from env + TOML. Code reads it via
  `get_settings()` at call time (never at import), so tests swap it with `set_settings()`.
- `ssh.py`: `ssh_manager.run(cmd, timeout=, input=, strip=)`. Success → `{"stdout": ...}`
  (+`stderr` if any); failure → `{..., "success": False}`. Test with `ok(result)`.
  Use `input=` for uploads (never put file content on the command line) and `strip=False`
  when exact content matters.
- `paths.py`: `check_path()` = lexical policy; `await guard(path, for_write)` = policy + remote
  `realpath`; use the returned resolved path.
- `server.py`: `build_tools()` (schemas are built from Settings), `_HANDLERS`, `MUTATING_TOOLS`,
  `build_instructions()`. Uses the mcp 2.x low-level `Server(on_list_tools=, on_call_tool=)` API.

## Rules

- Tool functions return `dict` and never raise; return `{"error": "..."}`.
- Every user-supplied value that reaches a shell goes through `shlex.quote` or a strict
  regex (`fullmatch`). No general command-execution tool.
- New mutating tools → add to `MUTATING_TOOLS` (and `_DESTRUCTIVE_TOOLS` if irreversible).
- Style: 100-char lines, `list[str]` / `X | None`, relative imports inside the package.
- mcp is pinned to 2.x (snake_case pydantic fields: `input_schema`, `read_only_hint`, …).
