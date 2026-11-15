import asyncio
import os
import time
from uuid import uuid4

import pytest
import pytest_asyncio

from sca.core.models import ExecExit, ExecOutput, Mount, SandboxSpec


@pytest_asyncio.fixture(name="sandbox")
async def sandbox_fixture(tmp_path, provider):
    name = f"test-sandbox-{uuid4().hex}"
    file = tmp_path / "single"
    file.write_text("single-file")
    spec = SandboxSpec(
        image="sca-sandbox:latest",
        cpus=0.1,
        memory_mb=128,
        network="none",
        env={"TEST_ENV": "value"},
        labels={"sca.test": "true"},
        mounts=[
            Mount(source=str(tmp_path / "work"), target="/workspace"),
            Mount(source=str(file), target="/single", read_only=True),
            Mount(source=str(file), target="/single-rw"),
        ],
    )
    try:
        sid = await provider.create(name, spec)
        yield provider, sid, name, spec
    finally:
        await provider.destroy(f"sca-{name}")


async def collect(provider, sid, argv, **kwargs):
    return [event async for event in provider.exec(sid, argv, **kwargs)]


async def test_create_destroy(sandbox):
    provider, sid, name, spec = sandbox
    assert await provider.create(name, spec) == sid
    assert await provider.exists(sid)
    await provider.destroy(sid)
    await provider.destroy(sid)
    assert not await provider.exists(sid)


async def test_stream_stdin_long_line_exit(sandbox):
    provider, sid, *_ = sandbox
    start = time.monotonic()
    events = provider.exec(sid, ["bash", "-lc", "echo first; sleep 2; echo last >&2; exit 7"])
    first = await anext(events)
    assert first.text == "first"
    assert time.monotonic() - start < 1.7
    rest = [event async for event in events]
    assert rest[-1] == ExecExit(exit_code=7)
    assert ExecOutput(stream="stderr", text="last") in rest
    assert sum(isinstance(event, ExecExit) for event in rest) == 1
    result = await collect(provider, sid, ["cat"], stdin=b"hello\nlast")
    assert [e.text for e in result if isinstance(e, ExecOutput)] == ["hello", "last"]
    result = await collect(provider, sid, ["python3", "-c", "print('x'*200000)"])
    assert result[0].text == "x" * 200000
    assert result[-1].exit_code == 0


async def test_timeout_and_close(sandbox):
    provider, sid, *_ = sandbox
    command = ["bash", "-lc", "echo $$ > /workspace/pid; echo ready; sleep 1000 & wait"]
    result = await collect(provider, sid, command, timeout_s=0.8)
    assert result[-1] == ExecExit(exit_code=124, timed_out=True)
    check = ["bash", "-lc", '! kill -0 "$(cat /workspace/pid)" 2>/dev/null']
    assert (await collect(provider, sid, check))[-1].exit_code == 0
    stream = provider.exec(sid, command)
    assert (await anext(stream)).text == "ready"
    await stream.aclose()
    assert (await collect(provider, sid, check))[-1].exit_code == 0

    async def consume():
        async for event in provider.exec(sid, command):
            if isinstance(event, ExecOutput):
                ready.set()

    ready = asyncio.Event()
    task = asyncio.create_task(consume())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await collect(provider, sid, check))[-1].exit_code == 0


async def test_env_network_mounts_ownership(sandbox):
    provider, sid, _, spec = sandbox
    result = await collect(
        provider,
        sid,
        [
            "bash",
            "-lc",
            (
                'printf "%s:%s:%s\\n" "$TEST_ENV" "$EXTRA" "$PWD"; '
                "cat /single; echo changed > /single-rw; touch owned; "
                "if echo forbidden > /single; then exit 9; fi"
            ),
        ],
        workdir="/workspace",
        env={"EXTRA": "extra"},
    )
    assert result[0].text == "value:extra:/workspace"
    assert result[-1].exit_code == 0
    from pathlib import Path

    work = Path(spec.mounts[0].source)
    assert (work / "owned").stat().st_uid == os.getuid()
    assert (work / "owned").stat().st_gid == os.getgid()
    assert Path(spec.mounts[2].source).read_text() == "changed\n"


async def test_network_none(sandbox):
    provider, sid, *_ = sandbox
    result = await collect(
        provider,
        sid,
        ["python3", "-c", "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)"],
        timeout_s=10,
    )
    assert result[-1].exit_code != 0
