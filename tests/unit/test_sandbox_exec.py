import asyncio
import os
import signal
import sys
from contextlib import aclosing, suppress
from pathlib import Path

import pytest

from sca.core.models import SandboxSpec
from sca.host import host_executable
from sca.sandbox._exec import stream_exec
from sca.sandbox.local import LocalSandboxProvider


@pytest.mark.parametrize("driver", ["container", "mounted-node", "local"])
async def test_cancel_during_exec_startup_kills_process_group(driver, tmp_path, monkeypatch):
    node = driver == "mounted-node"
    executable = host_executable("node") if node else Path(sys.executable)
    if executable is None:
        pytest.skip("Node.js is unavailable")
    interpreter = [str(executable), "--input-type=module", "-e"] if node else [str(executable), "-c"]
    create_subprocess = asyncio.create_subprocess_exec
    spawned = asyncio.Event()
    release_handle = asyncio.Event()
    proc = None

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
            return [*interpreter, wrapper, str(tmp_path / Path(marker).name), *argv]

        async def cleanup(script, marker):
            process = await create_subprocess(
                *interpreter, script, str(tmp_path / Path(marker).name)
            )
            assert await process.wait() == 0

        stream = stream_exec(command, cleanup, node=node)

    async with asyncio.timeout(5), aclosing(stream):
        pending = asyncio.create_task(anext(stream))
        try:
            await spawned.wait()
            pending.cancel("interrupt")
            await asyncio.sleep(0)
            assert not pending.done()
            release_handle.set()
            with pytest.raises(asyncio.CancelledError, match="interrupt"):
                await pending
            assert proc.returncode == -signal.SIGKILL or (node and proc.returncode == 128 + signal.SIGKILL)
            # The background child holds stdout open until the group is killed.
            assert await proc.stdout.read() == b""
            assert not list(tmp_path.glob("sca-exec-*"))
        finally:
            release_handle.set()
            if proc is not None and (proc.returncode is None or not proc.stdout.at_eof()):
                with suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            await asyncio.gather(pending, return_exceptions=True)
