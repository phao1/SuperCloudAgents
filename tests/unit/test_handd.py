import asyncio
import base64
import json
import sys
from unittest.mock import AsyncMock

import pytest

from sca.core.errors import SandboxError
from sca.core.models import ExecExit, ExecOutput
from sca.sandbox.docker import DockerSandboxProvider
from sca.sandbox.handd import DAEMON, HanddClient


@pytest.fixture
async def client():
    client = HanddClient([sys.executable, '-u', str(DAEMON)])
    await client.start()
    yield client
    await client.close()


async def collect(client, code, **kw):
    return [e async for e in client.exec([sys.executable, '-c', code], **kw)]


async def test_multiplex_binary_big_output(client):
    results = await asyncio.gather(*[
        collect(client, 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())',
                stdin=b'\xff\x00hello\n' + b'x' * 200000) for _ in range(4)
    ])
    for events in results:
        assert events == [ExecOutput(stream='stdout', text='�\x00hello'),
                          ExecOutput(stream='stdout', text='x' * 200000), ExecExit(exit_code=0)]


async def test_timeout_and_spawn_failure(client):
    assert (await collect(client, 'import time; time.sleep(30)', timeout_s=.05))[-1] == (
        ExecExit(exit_code=124, timed_out=True))
    assert [e async for e in client.exec(['/nonexistent/handd'])] == [ExecExit(exit_code=127)]


async def test_cancel_kills_group(client, tmp_path):
    target = tmp_path / 'escaped'
    stream = client.exec([sys.executable, '-u', '-c',
        ('import subprocess,sys,time; '
        'subprocess.Popen([sys.executable,"-c", '
        f'"import time,pathlib; time.sleep(.3); pathlib.Path({str(target)!r}).touch()"]); '
        'print("ready",flush=True); time.sleep(30)')])
    assert (await anext(stream)).text == 'ready'
    await stream.aclose()
    await asyncio.sleep(.5)
    assert not target.exists()


async def test_fragmented_frames_and_binary():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-u', str(DAEMON), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE)
    message = json.dumps({'id': 'one', 'argv': [sys.executable, '-c',
        'import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())'],
        'stdin': base64.b64encode(b'\xff\x00').decode()}).encode() + b'\n'
    for byte in message:
        proc.stdin.write(bytes([byte]))
        await proc.stdin.drain()
    reply = json.loads(await proc.stdout.readline())
    assert base64.b64decode(reply['stdout']) == b'\xff\x00'
    assert json.loads(await proc.stdout.readline())['exit'] == 0
    proc.stdin.close()
    assert await asyncio.wait_for(proc.wait(), 3) == 0


async def test_provider_start_failure_falls_back(monkeypatch):
    provider = DockerSandboxProvider()
    provider._handd_enabled = True
    provider._unpause = AsyncMock()
    monkeypatch.setattr(HanddClient, 'start', AsyncMock(side_effect=SandboxError('unavailable')))

    async def direct(*args, **kwargs):
        yield ExecExit(exit_code=0)

    monkeypatch.setattr('sca.sandbox.docker.stream_exec', direct)
    assert [e async for e in provider.exec('one', ['true'])] == [ExecExit(exit_code=0)]
    assert HanddClient.start.await_count == 2


async def test_provider_disconnect_does_not_replay(monkeypatch):
    provider = DockerSandboxProvider()
    provider._handd_enabled = True
    provider._unpause = AsyncMock()
    monkeypatch.setattr(HanddClient, 'start', AsyncMock())

    async def disconnected(*args, **kwargs):
        yield ExecOutput(stream='stdout', text='side effect')
        raise SandboxError('disconnected')

    monkeypatch.setattr(HanddClient, 'exec', disconnected)
    with pytest.raises(SandboxError):
        _ = [e async for e in provider.exec('one', ['true'])]


async def test_restart_dead_client(client):
    old = client.proc
    old.kill()
    await old.wait()
    await client.reader
    await client.start()
    assert client.proc.pid != old.pid
    assert await collect(client, 'pass') == [ExecExit(exit_code=0)]


async def test_cwd_env_stderr_and_lines(client, tmp_path):
    events = await collect(client,
        'import os,sys; print(os.getcwd()); print(os.environ["HANDD_TEST"]); '
        'sys.stderr.write("a\\nb\\ntrailing")',
        workdir=str(tmp_path), env={'HANDD_TEST': 'value'})
    assert [e.text for e in events if e.kind == 'output' and e.stream == 'stdout'] == [
        str(tmp_path), 'value']
    assert [e.text for e in events if e.kind == 'output' and e.stream == 'stderr'] == [
        'a', 'b', 'trailing']


async def test_provider_multiplex_blocks_pause(monkeypatch):
    provider = DockerSandboxProvider()
    provider._suspend_enabled = True
    provider._handd_enabled = True
    provider._unpause = AsyncMock()
    provider._cli = AsyncMock(return_value=(0, '', ''))
    monkeypatch.setattr(HanddClient, 'start', AsyncMock())
    entered = 0
    both = asyncio.Event()
    finish = asyncio.Event()

    async def execute(*args, **kwargs):
        nonlocal entered
        entered += 1
        if entered == 2:
            both.set()
        await finish.wait()
        yield ExecExit(exit_code=0)

    monkeypatch.setattr(HanddClient, 'exec', execute)

    async def run():
        return [e async for e in provider.exec('one', ['true'])]

    tasks = [asyncio.create_task(run()) for _ in range(2)]
    await asyncio.wait_for(both.wait(), 2)
    pause = asyncio.create_task(provider.pause('one'))
    await asyncio.sleep(0)
    assert not pause.done()
    finish.set()
    await asyncio.gather(*tasks, pause)
    provider._unpause.assert_awaited_once()
