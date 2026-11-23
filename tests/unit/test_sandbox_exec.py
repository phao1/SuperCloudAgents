import asyncio
import os
import signal
import sys
from contextlib import aclosing, suppress
from pathlib import Path

import pytest

from sca.core.models import SandboxSpec
from sca.host import host_executable
from sca.sandbox import local
from sca.sandbox._exec import stream_exec
from sca.sandbox.local import LocalSandboxProvider


@pytest.mark.parametrize("driver", ["container", "mounted-node", "local"])
async def test_cancel_during_exec_startup_kills_process_group(driver, tmp_path, monkeypatch):
    node = driver == "mounted-node"
    macos_node = node and sys.platform == "darwin"
    executable = host_executable("node") if node else Path(sys.executable)
    if executable is None:
        pytest.skip("Node.js is unavailable")
    interpreter = [str(executable), "--input-type=module", "-e"] if node else [str(executable), "-c"]
    create_subprocess = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    release_handle = asyncio.Event()
    proc = None
    marker_path = group_id = cleanup_code = None

    async def delayed_spawn(*args, **kwargs):
        nonlocal proc
        proc = await create_subprocess(*args, **kwargs)
        assert await proc.stdout.readline() == b"ready\n"
        spawned.set()
        await release_handle.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)
    argv = ["bash", "-c", "sleep 30 & echo ready; wait"]
    if driver == "local":
        provider = LocalSandboxProvider(tmp_path / "sandboxes")
        sandbox_id = await provider.create("task", SandboxSpec(image="unused"))
        stream = provider.exec(sandbox_id, argv)
    else:
        def command(wrapper, marker):
            nonlocal marker_path
            marker_path = tmp_path / Path(marker).name
            return [*interpreter, wrapper, str(marker_path), *argv]

        async def cleanup(script, marker):
            nonlocal cleanup_code
            process = await create_subprocess(
                *interpreter, script, str(tmp_path / Path(marker).name)
            )
            cleanup_code = await process.wait()
            assert cleanup_code == 0 or (macos_node and cleanup_code == 255)

        stream = stream_exec(command, cleanup, node=node)

    async with asyncio.timeout(5), aclosing(stream):
        pending = asyncio.create_task(anext(stream))
        try:
            await spawned.wait()
            if macos_node:
                assert marker_path is not None
                while not marker_path.exists():
                    await asyncio.sleep(.01)
                group_id = int(marker_path.read_text())
            pending.cancel("interrupt")
            await asyncio.sleep(0)
            assert not pending.done()
            release_handle.set()
            with pytest.raises(asyncio.CancelledError, match="interrupt"):
                await pending
            if group_id is not None and 255 in (proc.returncode, cleanup_code):
                # asyncio can lose the exit status to the macOS child watcher.
                while True:
                    try:
                        os.killpg(group_id, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(.01)
            else:
                assert proc.returncode == -signal.SIGKILL or (node and proc.returncode == 128 + signal.SIGKILL)
            # The background child holds stdout open until the group is killed.
            assert await proc.stdout.read() == b""
            assert not list(tmp_path.glob("sca-exec-*"))
        finally:
            release_handle.set()
            if proc is not None and (proc.returncode is None or not proc.stdout.at_eof()):
                with suppress(ProcessLookupError):
                    os.killpg(group_id or proc.pid, signal.SIGKILL)
                await proc.wait()
            await asyncio.gather(pending, return_exceptions=True)


async def test_local_cancellation_kills_a_running_group_without_waiting_for_timeout(tmp_path, monkeypatch):
    killed = asyncio.Event()
    stop = local._stop_process

    def stop_process(proc):
        stop(proc)
        killed.set()

    monkeypatch.setattr(local, "_stop_process", stop_process)
    provider = LocalSandboxProvider(tmp_path / "sandboxes")
    sandbox_id = await provider.create("task", SandboxSpec(image="unused"))
    stream = provider.exec(sandbox_id, ["bash", "-c", "sleep 120 & echo ready; wait"], timeout_s=180)
    async with asyncio.timeout(5), aclosing(stream):
        assert (await anext(stream)).text == "ready"
        waiting = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.wait_for(killed.wait(), .5)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, 1)
