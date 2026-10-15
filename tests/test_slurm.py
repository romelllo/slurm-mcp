"""Tests for tools/slurm.py and tools/workspaces.py."""

from __future__ import annotations

import pytest

from slurm_mcp.tools import slurm, workspaces


class TestJobIdValidation:
    @pytest.mark.parametrize("job_id", ["1; rm -rf ~", "abc", "", "123\n", "12 34"])
    async def test_rejects(self, fake_ssh, job_id) -> None:
        for fn in (slurm.get_job_details, slurm.cancel_job, slurm.hold_job, slurm.release_job):
            assert "Invalid" in (await fn(job_id))["error"]
        assert fake_ssh.calls == []

    @pytest.mark.parametrize("job_id", ["123", "123_4", "123+1"])
    async def test_accepts(self, fake_ssh, job_id) -> None:
        result = await slurm.get_job_details(job_id)
        assert result["job_id"] == job_id


class TestQueue:
    async def test_user_filter(self, fake_ssh) -> None:
        await slurm.get_queue()
        assert fake_ssh.commands[0].startswith("squeue -u alice ")

    async def test_all_users(self, fake_ssh) -> None:
        await slurm.get_queue(user_only=False)
        assert "-u " not in fake_ssh.commands[0]

    async def test_empty_queue_note(self, fake_ssh) -> None:
        fake_ssh.on("squeue", stdout="JOBID PARTITION")
        assert (await slurm.get_queue())["note"] == "No jobs in the queue."


class TestOwnership:
    async def test_cancel_foreign_job_refused(self, fake_ssh) -> None:
        fake_ssh.on("squeue", stdout="")
        result = await slurm.cancel_job("999")
        assert "not in the queue" in result["error"]
        assert not any(c.startswith("scancel") for c in fake_ssh.commands)

    async def test_cancel_own_job(self, fake_ssh) -> None:
        fake_ssh.on("squeue", stdout="999")
        await slurm.cancel_job("999")
        assert fake_ssh.commands[-1] == "scancel 999"

    async def test_hold_checks_ownership(self, fake_ssh) -> None:
        fake_ssh.on("squeue", stdout="")
        assert "error" in await slurm.hold_job("5")


class TestSubmit:
    async def test_submit_from_script_dir(self, fake_ssh) -> None:
        fake_ssh.on("cd ", stdout="Submitted batch job 4242")
        result = await slurm.submit_job("/scratch/alice/jobs/run.sbatch")
        assert result["job_id"] == "4242"
        assert (
            fake_ssh.commands[0]
            == "cd /scratch/alice/jobs && sbatch /scratch/alice/jobs/run.sbatch"
        )

    async def test_submit_outside_write_paths(self, fake_ssh) -> None:
        result = await slurm.submit_job("/home/alice/run.sh")
        assert "writable location" in result["error"]


class TestHistoryAndEfficiency:
    async def test_history_bounds(self, fake_ssh) -> None:
        assert "error" in await slurm.get_job_history(days=0)
        assert "error" in await slurm.get_job_history(state="FAILED; ls")

    async def test_history_command(self, fake_ssh) -> None:
        await slurm.get_job_history(days=3, state="FAILED,TIMEOUT")
        cmd = fake_ssh.commands[0]
        assert "-u alice -S now-3days" in cmd
        assert cmd.endswith("--state=FAILED,TIMEOUT")

    async def test_efficiency_falls_back_to_sacct(self, fake_ssh) -> None:
        fake_ssh.on("seff", stdout="", stderr="seff: command not found", success=False)
        fake_ssh.on("sacct", stdout="JobID State")
        result = await slurm.get_job_efficiency("7")
        assert result["stdout"] == "JobID State"


class TestResolveProject:
    async def test_lists_projects(self, use_settings, fake_ssh) -> None:
        use_settings(projects={"rna": "/scratch/alice/rna"}, default_project="rna")
        result = await slurm.resolve_project()
        assert result["default_project"] == "/scratch/alice/rna"

    async def test_resolves_alias(self, use_settings, fake_ssh) -> None:
        use_settings(projects={"rna": "/scratch/alice/rna"})
        fake_ssh.on("test -d", stdout="exists")
        result = await slurm.resolve_project("rna")
        assert result == {"alias": "rna", "path": "/scratch/alice/rna", "exists": True}

    async def test_unknown_alias(self, fake_ssh) -> None:
        assert "error" in await slurm.resolve_project("nope")


class TestWorkspaces:
    async def test_name_validation(self, fake_ssh) -> None:
        assert "error" in await workspaces.allocate_workspace("x; rm", 5)
        assert fake_ssh.calls == []

    async def test_days_bounds(self, use_settings, fake_ssh) -> None:
        use_settings(workspace_max_days=30)
        assert "error" in await workspaces.allocate_workspace("proj", 31)
        await workspaces.allocate_workspace("proj", 30)
        assert fake_ssh.commands[-1] == "ws_allocate proj 30"

    async def test_release_warns_without_delete(self, fake_ssh) -> None:
        result = await workspaces.release_workspace("proj")
        assert "NOT deleted" in result["warning"]
        await workspaces.release_workspace("proj", delete_data=True)
        assert fake_ssh.commands[-1] == "ws_release --delete-data proj"
