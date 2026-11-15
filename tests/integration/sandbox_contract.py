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


async def test_archive_round_trip(sandbox, tmp_path):
    import tempfile

    from sca.workspace.tarsafe import chunks, extract, pack, receive

    provider, sid, *_ = sandbox
    source = tmp_path / "upload"
    source.mkdir()
    (source / "binary").write_bytes(bytes(range(256)) * 1024)
    (source / "binary").chmod(0o755)
    (source / "link").symlink_to("binary")
    (source / "empty").mkdir()
    with tempfile.TemporaryFile() as tar:
        pack(source, tar)
        tar.seek(0)
        await provider.upload(sid, "/tmp/roundtrip", chunks(tar))
    with tempfile.TemporaryFile() as tar:
        await receive(provider.download(sid, "/tmp/roundtrip"), tar)
        extract(tar, tmp_path / "download")
    restored = tmp_path / "download/roundtrip"
    assert (restored / "binary").read_bytes() == (source / "binary").read_bytes()
    assert (restored / "binary").stat().st_mode & 0o111 == 0o111
    assert (restored / "link").is_symlink()
    assert (restored / "link").read_bytes() == (source / "binary").read_bytes()
    assert list((restored / "empty").iterdir()) == []
    result = await collect(
        provider,
        sid,
        [
            "sh",
            "-c",
            'test -w /tmp/roundtrip/binary && test "$(stat -c %u /tmp/roundtrip/binary)" = "$(id -u)"',
        ],
    )
    assert result[-1].exit_code == 0


async def test_archive_large_file(sandbox, tmp_path):
    import hashlib
    import tempfile
    import tracemalloc

    from sca.workspace.tarsafe import chunks, extract, pack, receive

    provider, sid, *_ = sandbox
    source = tmp_path / "large"
    source.mkdir()
    block = bytes(range(256)) * 256
    expected = hashlib.sha256()
    with (source / "large.bin").open("wb") as file:
        for _ in range(1600):
            file.write(block)
            expected.update(block)
    with tempfile.TemporaryFile() as tar:
        pack(source, tar)
        tar.seek(0)
        tracemalloc.start()
        try:
            await provider.upload(sid, "/tmp/large", chunks(tar))
            with tempfile.TemporaryFile() as downloaded:
                await receive(provider.download(sid, "/tmp/large"), downloaded)
                extract(downloaded, tmp_path / "received")
            _, peak = tracemalloc.get_traced_memory()
            assert peak < 20 * 1024 * 1024
        finally:
            tracemalloc.stop()
    actual = hashlib.sha256()
    with (tmp_path / "received/large/large.bin").open("rb") as file:
        while chunk := file.read(65536):
            actual.update(chunk)
    assert actual.digest() == expected.digest()
    assert (tmp_path / "received/large/large.bin").stat().st_size == 100 * 1024 * 1024


async def test_archive_missing_path(sandbox):
    from sca.core.errors import SandboxError

    provider, sid, *_ = sandbox
    with pytest.raises(SandboxError):
        async for _ in provider.download(sid, "/does-not-exist"):
            pass


async def test_archive_empty_directory(sandbox, tmp_path):
    import tempfile

    from sca.workspace.tarsafe import chunks, extract, pack, receive

    provider, sid, *_ = sandbox
    source = tmp_path / "empty-source"
    source.mkdir()
    with tempfile.TemporaryFile() as tar:
        pack(source, tar)
        tar.seek(0)
        await provider.upload(sid, "/tmp/empty-target", chunks(tar))
    with tempfile.TemporaryFile() as tar:
        await receive(provider.download(sid, "/tmp/empty-target"), tar)
        extract(tar, tmp_path / "empty-result")
    assert list((tmp_path / "empty-result/empty-target").iterdir()) == []


async def test_list_labels(sandbox):
    provider, sid, *_ = sandbox
    assert sid in await provider.list({"sca.test": "true"})
    assert sid in await provider.list({"sca.test": ""})
    assert sid not in await provider.list({"sca.test": "true", "sca.absent": "yes"})
