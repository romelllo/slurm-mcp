"""Tests for tools/filesystem.py."""

from __future__ import annotations

import shlex

from slurm_mcp.config import MAX_FILE_SIZE_BYTES, MAX_OUTPUT_LINES
from slurm_mcp.tools import filesystem as fs


class TestListDirectory:
    async def test_denied_path(self, fake_ssh) -> None:
        result = await fs.list_directory("/etc")
        assert "error" in result
        assert fake_ssh.calls == []

    async def test_lists(self, fake_ssh) -> None:
        fake_ssh.on("ls ", stdout="total 0\nfile.txt")
        result = await fs.list_directory("/scratch/alice")
        assert result["stdout"].endswith("file.txt")

    async def test_truncates(self, fake_ssh) -> None:
        fake_ssh.on("ls ", stdout="\n".join(f"l{i}" for i in range(MAX_OUTPUT_LINES + 50)))
        result = await fs.list_directory("/scratch/alice")
        assert result["truncated"] is True
        assert len(result["stdout"].splitlines()) == MAX_OUTPUT_LINES

    async def test_hostile_path_is_quoted(self, fake_ssh) -> None:
        nasty = "/scratch/alice/x'; touch /tmp/pwned; echo '"
        await fs.list_directory(nasty)
        # The whole path must survive as ONE shell word.
        assert shlex.split(fake_ssh.commands[0])[-1] == nasty


class TestReadFile:
    async def test_small_text_file(self, fake_ssh) -> None:
        fake_ssh.on("stat -c", stdout="12\nus-ascii")
        fake_ssh.on("cat ", stdout="  hello\n")
        result = await fs.read_file("/scratch/alice/a.txt")
        assert result["stdout"] == "  hello\n"  # not stripped
        assert result["size_bytes"] == 12

    async def test_large_file_previewed(self, fake_ssh) -> None:
        fake_ssh.on("stat -c", stdout=f"{MAX_FILE_SIZE_BYTES + 1}\nutf-8")
        fake_ssh.on("head ", stdout="first\n...\nlast")
        result = await fs.read_file("/scratch/alice/big.txt")
        assert result["warning"] == "large_file"
        assert "first" in result["preview"]
        assert not any(c.startswith("cat ") for c in fake_ssh.commands)

    async def test_binary_refused(self, fake_ssh) -> None:
        fake_ssh.on("stat -c", stdout="100\nbinary")
        result = await fs.read_file("/scratch/alice/x.bam")
        assert "binary" in result["error"]

    async def test_missing_file(self, fake_ssh) -> None:
        fake_ssh.on("stat -c", stdout="", stderr="No such file", success=False)
        result = await fs.read_file("/scratch/alice/nope")
        assert result["success"] is False


class TestHeadTail:
    async def test_head_decompresses(self, fake_ssh) -> None:
        await fs.head_file("/shared/data/r1.fastq.gz", lines=8)
        assert fake_ssh.commands[0].startswith("zcat -f -- ")
        assert fake_ssh.commands[0].endswith("head -n 8")

    async def test_line_bounds(self, fake_ssh) -> None:
        assert "error" in await fs.tail_file("/scratch/alice/log", lines=0)
        assert "error" in await fs.head_file("/scratch/alice/log", lines=5000)


class TestSearchAndFind:
    async def test_grep_pattern_quoted(self, fake_ssh) -> None:
        await fs.search_in_file("/scratch/alice/log", "it's -v")
        words = shlex.split(fake_ssh.commands[0])
        assert words[words.index("-e") + 1] == "it's -v"

    async def test_find_rejects_bad_size_filter(self, fake_ssh) -> None:
        result = await fs.find_files("/scratch/alice", size_filter="+1G; rm -rf ~")
        assert "error" in result

    async def test_find_rejects_bad_type(self, fake_ssh) -> None:
        assert "error" in await fs.find_files("/scratch/alice", file_type="x")

    async def test_find_builds_command(self, fake_ssh) -> None:
        await fs.find_files("/scratch/alice", "*.bam", "f", 2, "+1G")
        assert "-maxdepth 2 -type f -name '*.bam' -size +1G" in fake_ssh.commands[0]


class TestWriteFile:
    async def test_content_goes_over_stdin(self, fake_ssh) -> None:
        content = "#!/bin/bash\necho 'quotes' \"and\" $VARS\n" * 10_000  # > ARG_MAX chunk
        result = await fs.write_file("/scratch/alice/jobs/run.sh", content)
        assert "File written" in result["message"]
        call = [c for c in fake_ssh.calls if c["command"].startswith("mkdir")][0]
        assert call["input"] == content
        assert content not in call["command"]

    async def test_write_outside_denied(self, fake_ssh) -> None:
        result = await fs.write_file("/home/alice/x.sh", "x")
        assert "Write access denied" in result["error"]

    async def test_size_limit(self, fake_ssh, monkeypatch) -> None:
        monkeypatch.setattr(fs, "MAX_WRITE_SIZE_BYTES", 10)
        assert "limit" in (await fs.write_file("/scratch/alice/x", "x" * 11))["error"]


class TestEditFile:
    async def test_preserves_surrounding_whitespace(self, fake_ssh) -> None:
        fake_ssh.on("cat -- ", stdout="\n  a = 1\n  b = 2\n\n")
        result = await fs.edit_file("/scratch/alice/x.py", "a = 1", "a = 3")
        assert result["message"].startswith("Edited")
        write = fake_ssh.calls[-1]
        assert write["command"].startswith("cat > ")
        assert write["input"] == "\n  a = 3\n  b = 2\n\n"
        read = [c for c in fake_ssh.calls if c["command"].startswith("cat -- ")][0]
        assert read["strip"] is False

    async def test_ambiguous_match(self, fake_ssh) -> None:
        fake_ssh.on("cat -- ", stdout="x x")
        result = await fs.edit_file("/scratch/alice/f", "x", "y")
        assert "2 occurrences" in result["error"]

    async def test_replace_all(self, fake_ssh) -> None:
        fake_ssh.on("cat -- ", stdout="x x")
        result = await fs.edit_file("/scratch/alice/f", "x", "y", replace_all=True)
        assert fake_ssh.calls[-1]["input"] == "y y"
        assert "2 occurrences" in result["message"]

    async def test_not_found(self, fake_ssh) -> None:
        fake_ssh.on("cat -- ", stdout="abc")
        assert "not found" in (await fs.edit_file("/scratch/alice/f", "zzz", "y"))["error"]

    async def test_empty_old_string(self, fake_ssh) -> None:
        assert "error" in await fs.edit_file("/scratch/alice/f", "", "y")


class TestStorageUsage:
    async def test_skips_glob_roots(self, fake_ssh) -> None:
        fake_ssh.on("df ", stdout="Filesystem Size")
        result = await fs.storage_usage()
        assert result["df"] == "Filesystem Size"
        assert "/ws/alice-*" not in fake_ssh.commands[0]
        assert "/scratch/alice/" in fake_ssh.commands[0]
