"""
Generate SLURM batch scripts and R Markdown notebooks.
These functions only build text — saving and submitting are separate tools,
so the user can review the script first.
"""

import re
from datetime import datetime
from pathlib import PurePosixPath

from ..config import get_settings
from ..paths import check_path

_ARRAY_RE = re.compile(r"[0-9,\-:%]+")
_GPU_RE = re.compile(r"(?:[A-Za-z0-9_.\-]+:)?(\d+)")
_NODELIST_RE = re.compile(r"[A-Za-z0-9,\[\]\-_.]+")
_SAFE_VALUE_RE = re.compile(r"[A-Za-z0-9_.@+\-]+")


def parse_slurm_time(value: str) -> int | None:
    """
    Convert a SLURM time string to seconds. Accepts the sbatch --time formats:
    MM, MM:SS, HH:MM:SS, D-HH, D-HH:MM, D-HH:MM:SS. Returns None if invalid.
    """
    days, rest = 0, value.strip()
    if "-" in rest:
        day_part, rest = rest.split("-", 1)
        if not day_part.isdigit():
            return None
        days = int(day_part)
    parts = rest.split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if days or "-" in value:
        hours, minutes, seconds = (nums + [0, 0])[:3]
    elif len(nums) == 1:
        hours, minutes, seconds = 0, nums[0], 0
    elif len(nums) == 2:
        hours, minutes, seconds = 0, nums[0], nums[1]
    else:
        hours, minutes, seconds = nums
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def generate_sbatch_script(
    job_name: str,
    cpus: int,
    memory_gb: int,
    walltime: str,
    commands: list[str],
    output_dir: str,
    partition: str = "",
    qos: str = "",
    gpu: str = "",
    nodelist: str = "",
    account: str = "",
    array: str = "",
    email: str = "",
    mail_type: str = "END,FAIL",
    extra_directives: list[str] | None = None,
) -> dict:
    """Build an sbatch script and validate it against the limits in cluster.toml."""
    s = get_settings()
    errors: list[str] = []
    warnings: list[str] = []

    job_name = re.sub(r"[^\w.\-]", "_", job_name.strip()) or "job"
    partition = partition or s.default_partition
    account = account or s.account

    # ── Partition limits ──────────────────────────────────────────────────────
    part = s.partition(partition) if partition else None
    if s.partitions and partition and part is None:
        errors.append(
            f"Unknown partition '{partition}'. Known: {', '.join(p.name for p in s.partitions)}"
        )

    seconds = parse_slurm_time(walltime)
    if seconds is None:
        errors.append(f"Invalid walltime '{walltime}'. Use HH:MM:SS or D-HH:MM:SS.")

    gpu_count = 0
    if gpu:
        gpu_match = _GPU_RE.fullmatch(gpu)
        if not gpu_match:
            errors.append(f"Invalid gpu '{gpu}'. Use a count ('1') or type:count ('a100:1').")
        else:
            gpu_count = int(gpu_match.group(1))

    if part is not None:
        if part.max_cpus is not None and cpus > part.max_cpus:
            errors.append(f"cpus={cpus} exceeds '{part.name}' maximum of {part.max_cpus}.")
        if part.max_mem_gb is not None and memory_gb > part.max_mem_gb:
            errors.append(
                f"memory_gb={memory_gb} exceeds '{part.name}' maximum of {part.max_mem_gb} GB."
            )
        if gpu_count and part.max_gpus == 0:
            errors.append(f"Partition '{part.name}' has no GPUs.")
        elif part.max_gpus is not None and gpu_count > part.max_gpus:
            errors.append(f"{gpu_count} GPUs exceeds '{part.name}' maximum of {part.max_gpus}.")
        if part.max_time and seconds is not None:
            max_seconds = parse_slurm_time(part.max_time)
            if max_seconds is not None and seconds > max_seconds:
                errors.append(f"walltime {walltime} exceeds '{part.name}' limit {part.max_time}.")

    # ── QoS ───────────────────────────────────────────────────────────────────
    if qos and s.qos and qos not in s.qos:
        errors.append(f"Unknown QoS '{qos}'. Allowed: {', '.join(s.qos)}")
    if not qos:
        over = []
        if s.no_qos_max_cpus is not None and cpus > s.no_qos_max_cpus:
            over.append(f"cpus>{s.no_qos_max_cpus}")
        if s.no_qos_max_mem_gb is not None and memory_gb > s.no_qos_max_mem_gb:
            over.append(f"memory>{s.no_qos_max_mem_gb}GB")
        if s.no_qos_max_gpus is not None and gpu_count > s.no_qos_max_gpus:
            over.append(f"gpus>{s.no_qos_max_gpus}")
        if over:
            hint = f" Set qos to one of: {', '.join(s.qos)}." if s.qos else ""
            warnings.append(
                f"Request exceeds the default per-user limit ({', '.join(over)}); the job may "
                f"stay pending without a higher QoS.{hint}"
            )

    # ── Header values (must not smuggle newlines or extra directives) ─────────
    if cpus < 1 or memory_gb < 1:
        errors.append("cpus and memory_gb must be positive.")
    if array and not _ARRAY_RE.fullmatch(array):
        errors.append(f"Invalid array spec '{array}'. Example: '1-10' or '1-100%10'.")
    if nodelist and not _NODELIST_RE.fullmatch(nodelist):
        errors.append(f"Invalid nodelist '{nodelist}'.")
    for label, value in (("qos", qos), ("account", account), ("email", email)):
        if value and not _SAFE_VALUE_RE.fullmatch(value):
            errors.append(f"Invalid {label} '{value}'.")
    if mail_type and not re.fullmatch(r"[A-Z_,]+", mail_type):
        errors.append(f"Invalid mail_type '{mail_type}'.")
    for directive in extra_directives or []:
        if not directive.startswith("--") or "\n" in directive:
            errors.append(
                f"extra_directives entries must look like '--option=value': {directive!r}"
            )
    if path_error := check_path(output_dir, for_write=True):
        errors.append(f"output_dir: {path_error}")

    if errors:
        failure: dict = {"error": "\n".join(errors)}
        if warnings:
            failure["warnings"] = warnings
        return failure

    # ── Build script ──────────────────────────────────────────────────────────
    output_dir = output_dir.rstrip("/")
    log_stem = f"{output_dir}/{job_name}_%A_%a" if array else f"{output_dir}/{job_name}_%j"
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --output={log_stem}.out",
        f"#SBATCH --error={log_stem}.err",
    ]
    if partition:
        lines.append(f"#SBATCH --partition={partition}")
    lines += [
        "#SBATCH --nodes=1",
        "#SBATCH --ntasks=1",
        f"#SBATCH --cpus-per-task={cpus}",
        f"#SBATCH --mem={memory_gb}G",
        f"#SBATCH --time={walltime}",
    ]
    if qos:
        lines.append(f"#SBATCH --qos={qos}")
    if account:
        lines.append(f"#SBATCH --account={account}")
    if gpu:
        lines.append(f"#SBATCH --gres=gpu:{gpu}")
    if nodelist:
        lines.append(f"#SBATCH --nodelist={nodelist}")
    if array:
        lines.append(f"#SBATCH --array={array}")
    if email:
        lines.append(f"#SBATCH --mail-type={mail_type}")
        lines.append(f"#SBATCH --mail-user={email}")
    lines += [f"#SBATCH {d}" for d in extra_directives or []]

    lines += [
        "",
        'echo "Job ${SLURM_JOB_ID} started on $(hostname) at $(date)"',
        "",
    ]
    if s.preamble:
        lines += ["# ── Environment ──", *s.preamble, ""]
    lines += ["# ── Commands ──", *commands, "", 'echo "Job finished at $(date)"']

    result: dict = {
        "script": "\n".join(lines) + "\n",
        "suggested_path": f"{output_dir}/{job_name}_{datetime.now():%Y%m%d}.sbatch",
        "next_step": "Show the script to the user, save it with hpc_write_file, "
        "then submit with hpc_submit_job.",
    }
    if warnings:
        result["warnings"] = warnings
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# R MARKDOWN
# ═══════════════════════════════════════════════════════════════════════════════

_ANALYSIS_PACKAGES = {
    "QC": ["ggplot2", "dplyr", "tidyr", "DT", "knitr"],
    "DESeq2": ["DESeq2", "ggplot2", "pheatmap", "EnhancedVolcano", "dplyr"],
    "survival": ["survival", "survminer", "ggplot2", "dplyr"],
    "scRNA": ["Seurat", "ggplot2", "dplyr", "patchwork"],
    "general": ["ggplot2", "dplyr", "tidyr", "knitr", "DT"],
}


def generate_rmarkdown_notebook(
    title: str,
    author: str,
    data_paths: list[str],
    analysis_type: str,
    output_dir: str,
    r_packages: list[str] | None = None,
) -> dict:
    """R Markdown template. Data paths are referenced as variables, never loaded here."""
    packages = list(
        dict.fromkeys(_ANALYSIS_PACKAGES.get(analysis_type, _ANALYSIS_PACKAGES["general"]))
    )
    packages = list(dict.fromkeys(packages + (r_packages or [])))
    data_vars = "\n".join(f'path_{i + 1} <- "{p}"' for i, p in enumerate(data_paths))
    pkg_loads = "\n".join(f"library({p})" for p in packages)
    safe_title = title.replace('"', "'")
    safe_author = author.replace('"', "'")
    file_stem = re.sub(r"[^\w.\-]", "_", title)

    rmd = f"""---
title: "{safe_title}"
author: "{safe_author}"
date: "`r Sys.time()`"
output:
  html_document:
    toc: true
    toc_float: true
    code_folding: show
    df_print: paged
---

```{{r setup, include=FALSE}}
knitr::opts_chunk$set(echo = TRUE, warning = FALSE, message = FALSE,
                      fig.width = 10, fig.height = 7)
```

## Packages

```{{r packages}}
# Do not call install.packages() inside a rendered notebook. Install once,
# interactively, into your personal library or a conda environment.
{pkg_loads}
```

## Data paths

```{{r data-paths}}
{data_vars}

for (p in ls(pattern = "^path_")) {{
  f <- get(p)
  if (file.exists(f)) cat("OK:", f, "\\n") else warning("File not found: ", f)
}}
```

## Load data

```{{r load-data}}
# data <- readr::read_tsv(path_1)
```

## Analysis

```{{r analysis}}
```

## Figures

```{{r figures}}
```

## Session info

```{{r session-info}}
sessionInfo()
```
"""
    return {
        "content": rmd,
        "packages_included": packages,
        "suggested_path": f"{output_dir.rstrip('/')}/{file_stem}_{datetime.now():%Y%m%d}.Rmd",
        "next_step": "Save with hpc_write_file, then build a render job with "
        "hpc_generate_rmd_render_job.",
    }


def generate_r_render_script(
    rmd_path: str,
    output_dir: str,
    cpus: int = 4,
    memory_gb: int = 16,
    walltime: str = "04:00:00",
    partition: str = "",
    email: str = "",
) -> dict:
    """sbatch script that renders an R Markdown file using the [rmd] setup in cluster.toml."""
    s = get_settings()
    commands = [
        *s.rmd_setup,
        "",
        f"Rscript -e \"rmarkdown::render('{rmd_path}', output_dir='{output_dir}')\"",
    ]
    result = generate_sbatch_script(
        job_name=PurePosixPath(rmd_path).stem + "_render",
        cpus=cpus,
        memory_gb=memory_gb,
        walltime=walltime,
        commands=commands,
        output_dir=output_dir,
        partition=partition,
        email=email,
    )
    if not s.rmd_setup and "script" in result:
        result.setdefault("warnings", []).append(
            "cluster.toml has no [rmd] setup lines — make sure Rscript is on PATH in batch jobs."
        )
    return result
