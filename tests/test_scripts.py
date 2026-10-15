"""Tests for tools/scripts.py — sbatch generation and validation."""

from __future__ import annotations

import pytest

from slurm_mcp.tools.scripts import (
    generate_r_render_script,
    generate_rmarkdown_notebook,
    generate_sbatch_script,
    parse_slurm_time,
)

BASE = {
    "job_name": "align",
    "cpus": 8,
    "memory_gb": 32,
    "walltime": "12:00:00",
    "commands": ["bwa mem ref.fa r1.fq r2.fq > out.sam"],
    "output_dir": "/scratch/alice/logs",
}


def sbatch(**overrides) -> dict:
    return generate_sbatch_script(**{**BASE, **overrides})


class TestParseSlurmTime:
    @pytest.mark.parametrize(
        ("value", "seconds"),
        [
            ("30", 30 * 60),
            ("30:15", 30 * 60 + 15),
            ("01:02:03", 3723),
            ("2-00", 2 * 86400),
            ("1-12:30", 86400 + 12 * 3600 + 30 * 60),
            ("14-00:00:00", 14 * 86400),
        ],
    )
    def test_valid(self, value, seconds) -> None:
        assert parse_slurm_time(value) == seconds

    @pytest.mark.parametrize("value", ["", "abc", "1:2:3:4", "x-01:00:00", "1-"])
    def test_invalid(self, value) -> None:
        assert parse_slurm_time(value) is None


class TestGenerateSbatch:
    def test_basic_script(self) -> None:
        script = sbatch()["script"]
        assert script.startswith("#!/bin/bash\n")
        assert "#SBATCH --partition=cpu" in script  # default partition
        assert "#SBATCH --cpus-per-task=8" in script
        assert "#SBATCH --mem=32G" in script
        assert "#SBATCH --output=/scratch/alice/logs/align_%j.out" in script
        assert "bwa mem" in script

    def test_unknown_partition(self) -> None:
        assert "Unknown partition" in sbatch(partition="bigmem")["error"]

    def test_cpu_limit(self) -> None:
        assert "exceeds 'cpu' maximum" in sbatch(cpus=128)["error"]

    def test_walltime_limit(self) -> None:
        assert "exceeds 'gpu' limit" in sbatch(partition="gpu", walltime="3-00:00:00")["error"]

    def test_invalid_walltime(self) -> None:
        assert "Invalid walltime" in sbatch(walltime="soon")["error"]

    def test_gpu_on_cpu_partition(self) -> None:
        assert "has no GPUs" in sbatch(gpu="1")["error"]

    def test_gpu_typed(self) -> None:
        script = sbatch(partition="gpu", gpu="a100:2")["script"]
        assert "#SBATCH --gres=gpu:a100:2" in script

    def test_unknown_qos(self) -> None:
        assert "Unknown QoS" in sbatch(qos="vip")["error"]

    def test_no_qos_limit_warns(self) -> None:
        result = sbatch(cpus=48)
        assert "script" in result
        assert "high" in result["warnings"][0]

    def test_qos_silences_warning(self) -> None:
        result = sbatch(cpus=48, qos="high")
        assert "warnings" not in result
        assert "#SBATCH --qos=high" in result["script"]

    def test_output_dir_must_be_writable(self) -> None:
        assert "output_dir" in sbatch(output_dir="/home/alice/logs")["error"]

    def test_header_injection_blocked(self) -> None:
        assert "error" in sbatch(email="a@b.c\n#SBATCH --qos=vip")
        assert "error" in sbatch(nodelist="node1\nrm -rf ~")
        assert "error" in sbatch(extra_directives=["--x\n--y"])

    def test_job_name_sanitised(self) -> None:
        assert "#SBATCH --job-name=my_job_1" in sbatch(job_name="my job/1")["script"]

    def test_array_logs(self) -> None:
        script = sbatch(array="1-10%2")["script"]
        assert "#SBATCH --array=1-10%2" in script
        assert "align_%A_%a.out" in script

    def test_preamble(self, use_settings) -> None:
        use_settings(preamble=("module load bwa",))
        script = sbatch()["script"]
        assert script.index("module load bwa") < script.index("bwa mem")

    def test_no_partitions_configured(self, use_settings) -> None:
        use_settings(partitions=(), default_partition="")
        script = sbatch(partition="anything")["script"]
        assert "#SBATCH --partition=anything" in script

    def test_account_default(self, use_settings) -> None:
        use_settings(account="lab42")
        assert "#SBATCH --account=lab42" in sbatch()["script"]


class TestRmd:
    def test_notebook(self) -> None:
        result = generate_rmarkdown_notebook(
            title='My "QC"',
            author="A",
            data_paths=["/shared/data/x.tsv"],
            analysis_type="QC",
            output_dir="/scratch/alice/",
            r_packages=["data.table"],
        )
        assert "title: \"My 'QC'\"" in result["content"]
        assert 'path_1 <- "/shared/data/x.tsv"' in result["content"]
        assert "library(data.table)" in result["content"]
        assert result["suggested_path"].startswith("/scratch/alice/My__QC__")

    def test_render_job_uses_setup(self, use_settings) -> None:
        use_settings(rmd_setup=("conda activate r-env",))
        result = generate_r_render_script("/scratch/alice/a.Rmd", "/scratch/alice/out")
        assert "conda activate r-env" in result["script"]
        assert "rmarkdown::render('/scratch/alice/a.Rmd'" in result["script"]
        assert "warnings" not in result

    def test_render_job_warns_without_setup(self) -> None:
        result = generate_r_render_script("/scratch/alice/a.Rmd", "/scratch/alice/out")
        assert "Rscript" in result["warnings"][0]
