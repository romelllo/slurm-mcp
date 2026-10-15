"""
SLURM job management.
Read operations plus submit/cancel/hold/release of the user's own jobs.
"""

import posixpath
import re
import shlex

from ..config import MAX_OUTPUT_LINES, get_settings
from ..paths import guard
from ..ssh import ok, ssh_manager

# 12345, 12345_7 (array task), 12345+1 (het job component)
_JOB_ID_RE = re.compile(r"\d+([_+]\d+)?")

_QUEUE_FORMAT = "%.18i %.9P %.30j %.10u %.8T %.10M %.10l %.5C %.8m %.6D %R"


def _bad_job_id(job_id: str) -> dict | None:
    if not _JOB_ID_RE.fullmatch(job_id):
        return {"error": f"Invalid SLURM job ID: {job_id!r}"}
    return None


def _truncate(result: dict) -> dict:
    lines = result.get("stdout", "").splitlines()
    if len(lines) > MAX_OUTPUT_LINES:
        result["stdout"] = "\n".join(lines[:MAX_OUTPUT_LINES])
        result["truncated"] = True
        result["note"] = f"Output truncated to {MAX_OUTPUT_LINES} lines."
    return result


async def _owned_by_user(job_id: str) -> dict | None:
    """Return an error dict unless the job is in the queue and belongs to HPC_USER."""
    user = get_settings().user
    check = await ssh_manager.run(f"squeue -h -j {job_id} -u {shlex.quote(user)} -o %i 2>/dev/null")
    if not check.get("stdout", "").strip():
        return {
            "error": f"Job {job_id} is not in the queue for user {user}. "
            "Only your own queued/running jobs can be changed."
        }
    return None


# ── Read-only ─────────────────────────────────────────────────────────────────


async def get_queue(user_only: bool = True) -> dict:
    """Current jobs in the queue."""
    user_filter = f"-u {shlex.quote(get_settings().user)} " if user_only else ""
    result = await ssh_manager.run(f"squeue {user_filter}-o '{_QUEUE_FORMAT}'")
    if ok(result) and len(result["stdout"].splitlines()) <= 1:
        result["note"] = "No jobs in the queue."
    return _truncate(result)


async def get_cluster_info(per_node: bool = False) -> dict:
    """Partitions and node availability (sinfo)."""
    if per_node:
        # One line per node: CPUs (alloc/idle/other/total), memory, GPUs, state.
        cmd = "sinfo -N -o '%.15N %.10P %.15C %.10m %.10e %.25G %.8t'"
    else:
        cmd = "sinfo -o '%.12P %.5a %.12l %.6D %.8t %.15C %N'"
    return _truncate(await ssh_manager.run(cmd))


async def get_job_details(job_id: str) -> dict:
    """scontrol (live jobs) + sacct (finished jobs) for one job."""
    if bad := _bad_job_id(job_id):
        return bad
    running = await ssh_manager.run(f"scontrol show job {job_id}")
    finished = await ssh_manager.run(
        f"sacct -j {job_id} --format=JobID,JobName%30,State,Elapsed,ReqMem,MaxRSS,"
        "AllocCPUS,CPUTime,NNodes,NodeList,Partition,ExitCode,Timelimit"
    )
    return {
        "job_id": job_id,
        "scontrol": running.get("stdout") or running.get("stderr", ""),
        "sacct": finished.get("stdout") or finished.get("stderr", ""),
    }


async def get_job_efficiency(job_id: str) -> dict:
    """CPU and memory efficiency of a finished job (seff if installed, else sacct)."""
    if bad := _bad_job_id(job_id):
        return bad
    seff = await ssh_manager.run(f"seff {job_id}")
    if ok(seff) and seff.get("stdout"):
        return seff
    return await ssh_manager.run(
        f"sacct -j {job_id} --units=G "
        "--format=JobID,State,Elapsed,AllocCPUS,TotalCPU,CPUTime,ReqMem,MaxRSS%12"
    )


async def get_job_history(days: int = 7, state: str = "") -> dict:
    """The user's jobs from the last N days (sacct)."""
    if not 1 <= days <= 90:
        return {"error": "days must be between 1 and 90."}
    if state and not re.fullmatch(r"[A-Za-z_,]+", state):
        return {"error": "state must be a comma-separated list like 'FAILED,TIMEOUT'."}
    cmd = (
        f"sacct -X -u {shlex.quote(get_settings().user)} -S now-{days}days "
        "--format=JobID,JobName%30,Partition,State,Elapsed,AllocCPUS,ReqMem,ExitCode,End"
    )
    if state:
        cmd += f" --state={state}"
    return _truncate(await ssh_manager.run(cmd))


async def list_reservations() -> dict:
    """SLURM reservations."""
    return await ssh_manager.run("scontrol show reservations")


# ── Mutating ──────────────────────────────────────────────────────────────────


async def submit_job(script_path: str) -> dict:
    """
    sbatch a script that already exists in a writable location.
    Runs from the script's directory so relative paths in the script work.
    """
    error, script_path = await guard(script_path, for_write=True)
    if error:
        return {"error": f"Job scripts must live in a writable location. {error}"}

    directory = posixpath.dirname(script_path) or "/"
    result = await ssh_manager.run(
        f"cd {shlex.quote(directory)} && sbatch {shlex.quote(script_path)}", timeout=60
    )
    if ok(result):
        match = re.search(r"Submitted batch job (\d+)", result["stdout"])
        if match:
            result["job_id"] = match.group(1)
    return result


async def cancel_job(job_id: str) -> dict:
    """Cancel one of the user's own jobs."""
    if bad := _bad_job_id(job_id):
        return bad
    if not_owned := await _owned_by_user(job_id):
        return not_owned
    return await ssh_manager.run(f"scancel {job_id}")


async def hold_job(job_id: str) -> dict:
    """Hold one of the user's own pending jobs."""
    if bad := _bad_job_id(job_id):
        return bad
    if not_owned := await _owned_by_user(job_id):
        return not_owned
    return await ssh_manager.run(f"scontrol hold {job_id}")


async def release_job(job_id: str) -> dict:
    """Release one of the user's own held jobs."""
    if bad := _bad_job_id(job_id):
        return bad
    if not_owned := await _owned_by_user(job_id):
        return not_owned
    return await ssh_manager.run(f"scontrol release {job_id}")


# ── Projects ──────────────────────────────────────────────────────────────────


async def resolve_project(alias: str = "") -> dict:
    """
    Resolve a project alias from HPC_PROJECTS to a directory.
    Without an alias, list all configured projects and the writable roots.
    """
    s = get_settings()
    if not alias:
        default = s.projects.get(s.default_project, "")
        return {
            "projects": s.projects,
            "default_project": default or None,
            "writable_paths": list(s.write_paths),
            "readable_paths": list(s.read_paths),
            "hint": "Configure aliases with HPC_PROJECTS=alias:/path,alias2:relative/dir in .env",
        }

    path = alias if alias.startswith("/") else s.projects.get(alias)
    if path is None:
        return {"error": f"Unknown project alias {alias!r}.", "projects": s.projects}
    check = await ssh_manager.run(f"test -d {shlex.quote(path)} && echo exists || echo missing")
    return {"alias": alias, "path": path, "exists": check.get("stdout", "").strip() == "exists"}
