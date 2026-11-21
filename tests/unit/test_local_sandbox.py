import asyncio
import io
import sys
import tarfile
from contextlib import aclosing
from pathlib import Path

import pytest

from sca.core.errors import SandboxError
from sca.core.models import ExecExit, ExecOutput, SandboxSpec
from sca.sandbox.local import LocalSandboxProvider
from sca.workspace.git import GitWorkspace
from sca.workspace.tarsafe import chunks, extract, pack, receive
from sca.workspace.transport import SyncTransport


@pytest.fixture
async def sandbox(tmp_path: Path):
    provider = LocalSandboxProvider(tmp_path / "sandboxes")
    await provider.create("task-1", SandboxSpec(
        image="unused-image", env={"EXPLICIT": "original"}, labels={"sca.task_id": "1"},
    ))
    return provider


async def test_exec_io_and_scrubbed_environment(sandbox, monkeypatch):
    monkeypatch.setenv("AMBIENT_TOKEN", "must-stay-on-host")
    sandbox_dir = sandbox.root / "task-1"
    workdir = sandbox_dir / "workspace" / "subdir"
    workdir.mkdir()
    program = """
import os
import sys

print(os.getcwd())
print(os.environ['HOME'])
print(os.environ['PATH'].split(os.pathsep)[0])
print(os.environ['EXPLICIT'])
print('AMBIENT_TOKEN' in os.environ)
print(sys.stdin.read(), file=sys.stderr)
"""
    result = [event async for event in sandbox.exec(
        "task-1", [sys.executable, "-c", program],
        workdir="/workspace/subdir", env={"EXPLICIT": "overridden", "HOME": "/root"},
        stdin=b"input",
    )]
    assert result[-1] == ExecExit(exit_code=0)
    assert [event.text for event in result[:-1] if event.stream == "stdout"] == [
        str(workdir), str(sandbox_dir / "agent_home"), str(sandbox_dir / "bin"), "overridden", "False",
    ]
    assert ExecOutput(stream="stderr", text="input") in result
    shell = [event async for event in sandbox.exec(
        "task-1", ["bash", "-lc", 'printf "%s\n" "$HOME"']
    )]
    assert shell[0] == ExecOutput(stream="stdout", text=str(sandbox_dir / "agent_home"))


async def test_timeout_kills_the_process_group(sandbox):
    async with asyncio.timeout(5):
        result = [event async for event in sandbox.exec(
            "task-1", ["bash", "-lc", "sleep 30 & echo ready; wait"], timeout_s=0.3,
        )]
    assert result == [
        ExecOutput(stream="stdout", text="ready"), ExecExit(exit_code=124, timed_out=True),
    ]


async def test_destroy_and_generator_close_stop_active_commands(sandbox):
    for action in ("close", "destroy"):
        async with asyncio.timeout(5), aclosing(sandbox.exec(
            "task-1", ["bash", "-lc", "echo ready; sleep 30"]
        )) as command:
            assert await anext(command) == ExecOutput(stream="stdout", text="ready")
            if action == "destroy":
                await sandbox.destroy("task-1")
                assert (await anext(command)).exit_code != 0
    await sandbox.destroy("task-1")
    assert not await sandbox.exists("task-1")
    assert not (sandbox.root / "task-1").exists()


async def test_durable_names_and_file_transfer(sandbox, tmp_path):
    assert await sandbox.create("task-1", SandboxSpec(image="ignored")) == "task-1"
    restarted = LocalSandboxProvider(sandbox.root)
    assert await restarted.list({"sca.task_id": ""}) == ["task-1"]
    assert await restarted.list({"sca.task_id": "other"}) == []
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("contents")
    archive = io.BytesIO()
    pack(source, archive)
    archive.seek(0)
    await restarted.upload("task-1", "/workspace", chunks(archive))

    for name in ("/workspace/.", "/workspace/file.txt"):
        buffer = io.BytesIO()
        await receive(restarted.download("task-1", name), buffer)
        extract(buffer, tmp_path / "output")
        assert (tmp_path / "output" / "file.txt").read_text() == "contents"

    outside = tmp_path / "outside"
    outside.write_text("outside")
    (sandbox.root / "task-1" / "workspace" / "link").symlink_to(outside)
    with pytest.raises(SandboxError, match="escapes"):
        _ = [chunk async for chunk in sandbox.download("task-1", "/workspace/link")]
    with pytest.raises(SandboxError, match="Invalid local sandbox name"):
        await sandbox.destroy("../outside")
    invalid = io.BytesIO()
    with tarfile.open(fileobj=invalid, mode="w") as tar:
        tar.add(outside, arcname="../outside")
    invalid.seek(0)
    with pytest.raises(SandboxError, match="Could not extract"):
        await restarted.upload("task-1", "/workspace", chunks(invalid))
    assert outside.read_text() == "outside"


async def test_local_workspace_checkpoint_uses_git_bundle(sandbox, best_of_repo, tmp_path):
    workspace = GitWorkspace(tmp_path / "task-state", "sca/checkpoint")
    before = await workspace.prepare(str(best_of_repo.task_dir), "main")
    transport = SyncTransport(sandbox)
    await transport.push("task-1", workspace.task_dir)
    result = [event async for event in sandbox.exec(
        "task-1", ["bash", "-lc", "printf 'updated\n' > result.txt"]
    )]
    assert result[-1].exit_code == 0
    result = [event async for event in sandbox.exec(
        "task-1", workspace.commit_argv("Update from local sandbox")
    )]
    assert result[-1].exit_code == 0
    commit = await transport.pull_commit("task-1", workspace)
    assert commit != before.base_sha
    assert await workspace._git("--git-dir", str(workspace.mirror), "show", f"{commit}:result.txt") == "updated\n"
    assert not (workspace.workspace / "result.txt").exists()
