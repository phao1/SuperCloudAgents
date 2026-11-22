import asyncio
import io
import os
import signal
import sys
import tarfile
from contextlib import aclosing, suppress
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
    provider = LocalSandboxProvider(tmp_path / "local sandboxes")
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
        "task-1", ["bash", "-lc", 'command -v python3 >/dev/null && printf "%s\n" "$1"',
                   "sca-test", "/agent_home"],
    )]
    expected = sandbox_dir / "agent_home" if sys.platform == "win32" else Path("/agent_home")
    assert shell[-1] == ExecExit(exit_code=0)
    assert Path(shell[0].text) == expected


async def test_timeout_kills_the_process_group(sandbox):
    async with asyncio.timeout(12):
        result = [event async for event in sandbox.exec(
            "task-1", ["bash", "-lc", "sleep 30 & echo ready; wait"],
            timeout_s=3 if sys.platform == "win32" else 0.3,
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


@pytest.mark.skipif(sys.platform == "win32", reason="Exercises POSIX process groups and pipes")
@pytest.mark.parametrize("action", ["close", "destroy"])
async def test_posix_cleanup_closes_pipes_even_if_a_descendant_keeps_them_open(sandbox, action):
    program = """
import subprocess, sys, time

child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                         start_new_session=True)
print(child.pid, flush=True)
time.sleep(30)
"""
    child_pid = None
    try:
        async with asyncio.timeout(5), aclosing(sandbox.exec(
            "task-1", [sys.executable, "-c", program], stdin=b"x" * 1024**2,
        )) as command:
            child_pid = int((await anext(command)).text)
            if action == "destroy":
                await sandbox.destroy("task-1")
                assert await anext(command) == ExecExit(exit_code=-signal.SIGKILL)
    finally:
        if child_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


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
    with pytest.raises(SandboxError, match="Invalid local sandbox name"):
        await sandbox.destroy("../outside")
    invalid = io.BytesIO()
    with tarfile.open(fileobj=invalid, mode="w") as tar:
        tar.add(outside, arcname="../outside")
    invalid.seek(0)
    with pytest.raises(SandboxError, match="Could not extract"):
        await restarted.upload("task-1", "/workspace", chunks(invalid))
    assert outside.read_text() == "outside"


@pytest.mark.skipif(sys.platform == "win32", reason="Creating this symlink requires Developer Mode")
async def test_download_cannot_follow_symlink_outside_the_sandbox(sandbox, tmp_path):
    outside = tmp_path / "outside"
    outside.touch()
    (sandbox.root / "task-1" / "workspace" / "link").symlink_to(outside)
    with pytest.raises(SandboxError, match="escapes"):
        _ = [chunk async for chunk in sandbox.download("task-1", "/workspace/link")]


@pytest.mark.skipif(sys.platform != "win32", reason="Exercises a Windows Job Object")
async def test_windows_job_reaps_background_child_after_success(sandbox):
    from scripts.portal_source import process_identity

    program = """
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
"""
    async with asyncio.timeout(15):
        results = [event async for event in sandbox.exec(
            "task-1", [sys.executable, "-c", program], timeout_s=10,
        )]
    assert results[-1] == ExecExit(exit_code=0)
    assert process_identity(int(results[0].text)) is None


async def test_local_workspace_checkpoint_uses_git_bundle(sandbox, best_of_repo, tmp_path):
    repo = best_of_repo.task_dir.rename(tmp_path / "repo with spaces")
    workspace = GitWorkspace(tmp_path / "task state", "sca/checkpoint")
    before = await workspace.prepare(repo.as_posix(), "main")
    assert (await workspace._git("-C", str(workspace.workspace), "config", "--local",
                                 "core.autocrlf")).strip() == "false"
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
