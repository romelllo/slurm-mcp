"""Tests for tools/docs.py and the server's tool registry / dispatcher."""

from __future__ import annotations

import json

import pytest
from mcp import types

from slurm_mcp import server
from slurm_mcp.tools import docs


@pytest.fixture
def cluster_docs(tmp_path, use_settings):
    (tmp_path / "00-overview.md").write_text("# Site overview\nScratch is purged after 30 days.\n")
    (tmp_path / "slurm-basics.md").write_text("# Site SLURM rules\nOverride.\n")
    use_settings(docs_dir=tmp_path)
    return tmp_path


class TestDocs:
    def test_lists_cluster_and_builtin(self, cluster_docs) -> None:
        listed = {d["file"]: d for d in docs.list_docs()["documents"]}
        assert listed["00-overview.md"]["source"] == "cluster"
        # cluster file with the same name wins over the builtin one
        assert listed["slurm-basics.md"]["title"] == "Site SLURM rules"

    def test_builtin_only_has_note(self) -> None:
        result = docs.list_docs()
        assert [d["file"] for d in result["documents"]] == ["slurm-basics.md"]
        assert "No cluster-specific docs" in result["note"]

    def test_read_partial_name(self, cluster_docs) -> None:
        assert docs.read_doc("overview")["content"].startswith("# Site overview")

    def test_read_missing(self, cluster_docs) -> None:
        assert "available" in docs.read_doc("nope")

    def test_search(self, cluster_docs) -> None:
        result = docs.search_docs("PURGED")
        assert result["total_files_matched"] == 1
        assert result["results"][0]["file"] == "00-overview.md"


class TestRegistry:
    def test_workspace_tools_only_when_enabled(self, use_settings) -> None:
        names = {t.name for t in server.build_tools()}
        assert "hpc_allocate_workspace" not in names
        use_settings(workspaces_enabled=True)
        names = {t.name for t in server.build_tools()}
        assert {"hpc_list_workspaces", "hpc_release_workspace"} <= names

    def test_every_tool_has_a_handler(self, use_settings) -> None:
        use_settings(workspaces_enabled=True)
        for tool in server.build_tools():
            assert tool.name in server._HANDLERS

    def test_annotations(self) -> None:
        tools = {t.name: t for t in server.build_tools()}
        assert tools["hpc_read_file"].annotations.read_only_hint is True  # type: ignore[union-attr]
        assert tools["hpc_submit_job"].annotations.read_only_hint is False  # type: ignore[union-attr]
        assert tools["hpc_cancel_job"].annotations.destructive_hint is True  # type: ignore[union-attr]

    def test_partition_enum_from_config(self) -> None:
        tool = next(t for t in server.build_tools() if t.name == "hpc_generate_sbatch")
        assert tool.input_schema["properties"]["partition"]["enum"] == ["cpu", "gpu"]

    def test_instructions_mention_cluster(self) -> None:
        text = server.build_instructions()
        assert "Test Cluster" in text
        assert "/scratch/alice/" in text
        assert "hpc_submit_job" in text


class TestDispatch:
    async def test_unknown_tool(self) -> None:
        assert "Unknown tool" in (await server.dispatch("hpc_nope", {}))["error"]

    async def test_disabled_workspace_tool(self) -> None:
        assert "Unknown tool" in (await server.dispatch("hpc_list_workspaces", {}))["error"]

    async def test_missing_argument(self) -> None:
        result = await server.dispatch("hpc_read_doc", {})
        assert result == {"error": "Missing required argument: filename"}

    async def test_bad_argument(self) -> None:
        result = await server.dispatch("hpc_generate_sbatch", {"bogus": 1})
        assert "Invalid arguments" in result["error"]

    async def test_sync_handler(self) -> None:
        assert "documents" in await server.dispatch("hpc_list_docs", {})

    async def test_call_tool_marks_errors(self, fake_ssh) -> None:
        fake_ssh.on("squeue", stdout="", stderr="slurm down", success=False)
        params = types.CallToolRequestParams(name="hpc_queue", arguments={})
        result = await server._on_call_tool(None, params)  # type: ignore[arg-type]
        assert result.is_error is True
        assert json.loads(result.content[0].text)["stderr"] == "slurm down"  # type: ignore[union-attr]


class TestCheck:
    async def test_reports_paths(self, fake_ssh, capsys) -> None:
        fake_ssh.on("hostname", stdout="login01\nslurm 23.11.4")
        fake_ssh.on("test -d '/scratch/alice/'", stdout="ok w")
        fake_ssh.on("test -d /scratch/alice/", stdout="ok w")
        fake_ssh.on("test -d /home/alice/", stdout="ok")
        fake_ssh.on("test -d /shared/data/", stdout="")
        status = await server.check()
        out = capsys.readouterr().out
        assert "logged in to login01, slurm 23.11.4" in out
        assert "✓ /scratch/alice/ (writable)" in out
        assert "✗ /shared/data/ does not exist" in out
        assert status == 1

    async def test_connection_failure(self, fake_ssh, capsys) -> None:
        fake_ssh.on("hostname", stdout="", stderr="Connection refused", success=False)
        assert await server.check() == 1
        assert "Connection refused" in capsys.readouterr().out
