import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from sca.core.errors import InvalidTaskError
from sca.core.models import Mount, SandboxSpec
from sca.sandbox.docker import DockerSandboxProvider
from sca.workspace.git import GitWorkspace

pytestmark = pytest.mark.integration


async def git(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return out.decode().strip()


async def source_repo(path: Path) -> Path:
    await git("init", "-b", "main", str(path))
    (path / "hello").write_text("before\n")
    await git("-C", str(path), "add", ".")
    await git(
        "-C",
        str(path),
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "initial",
    )
    await git("-C", str(path), "tag", "baseline")
    return path


async def test_round_trip_and_untrusted_config(tmp_path):
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
    except (OSError, TimeoutError):
        pytest.skip("Docker is unreachable")
    if proc.returncode:
        pytest.skip("Docker is unreachable")
    source = await source_repo(tmp_path / "source")
    ws = GitWorkspace(tmp_path / "task", "sca/test")
    prepared = await ws.prepare(str(source), "baseline")
    assert prepared.workspace_path == ws.workspace
    assert prepared.branch == "sca/test"
    provider = DockerSandboxProvider()
    name = f"test-workspace-{uuid4().hex}"
    try:
        sid = await provider.create(
            name,
            SandboxSpec(
                image="sca-sandbox:latest",
                mounts=[Mount(source=str(ws.workspace), target="/workspace")],
            ),
        )

        async def run(argv):
            events = [event async for event in provider.exec(sid, argv, workdir="/workspace")]
            assert events[-1].exit_code == 0, events

        await run(["bash", "-lc", "echo after > hello; printf '\\000\\377' > binary"])
        await run(ws.commit_argv('message with " quotes and $(touch injected)'))
        await run(ws.commit_argv("empty checkpoint"))
        assert not (ws.workspace / "injected").exists()
        sentinel = tmp_path / "host-executed"
        hook = ws.workspace / ".git/hooks/pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
        hook.chmod(0o755)
        monitor = ws.workspace / ".git/evil-monitor"
        monitor.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
        monitor.chmod(0o755)
        with (ws.workspace / ".git/config").open("a") as config:
            config.write(f"\n[core]\n fsmonitor = {monitor}\n")
        # Commit must disable the planted hooks and monitor inside the sandbox, too.
        await run(["bash", "-lc", "echo extra > extra"])
        await run(ws.commit_argv("safe checkpoint"))
        head = await ws.fetch()
        assert head != prepared.base_sha
        patch = await ws.diff(prepared.base_sha)
        assert "+after" in patch and "GIT binary patch" in patch
        assert await ws.changed(prepared.base_sha)
        assert not await ws.changed(head)
        destination = tmp_path / "destination.git"
        await git("init", "--bare", str(destination))
        await ws.push(str(destination))
        assert await git("--git-dir", str(destination), "rev-parse", "sca/test") == head
        assert not sentinel.exists()
        again = await ws.prepare(str(source), prepared.base_sha)
        assert again.base_sha == prepared.base_sha
        assert (ws.workspace / "hello").read_text() == "before\n"
    finally:
        await provider.destroy(f"sca-{name}")


async def test_invalid_repo_and_ref(tmp_path):
    ws = GitWorkspace(tmp_path / "task", "sca/test")
    with pytest.raises(InvalidTaskError):
        await ws.prepare(str(tmp_path / "missing"), "main")
    source = await source_repo(tmp_path / "source")
    with pytest.raises(InvalidTaskError):
        await ws.prepare(str(source), "missing-ref")


async def test_host_mirror_operations_with_untrusted_workspace(tmp_path):
    source = await source_repo(tmp_path / "source")
    ws = GitWorkspace(tmp_path / "task", "sca/host")
    prepared = await ws.prepare(str(source), "main")
    sentinel = tmp_path / "host-executed"
    monitor = ws.workspace / ".git/monitor"
    monitor.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    monitor.chmod(0o755)
    hook = ws.workspace / ".git/hooks/pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    hook.chmod(0o755)
    with (ws.workspace / ".git/config").open("a") as config:
        config.write(f"\n[core]\n fsmonitor = {monitor}\n")
    assert await ws.fetch() == prepared.base_sha
    assert await ws.diff(prepared.base_sha) == ""
    assert not await ws.changed(prepared.base_sha)
    destination = tmp_path / "destination.git"
    await git("init", "--bare", str(destination))
    await ws.push(str(destination))
    assert await git("--git-dir", str(destination), "rev-parse", ws.branch) == prepared.base_sha
    assert not sentinel.exists()
