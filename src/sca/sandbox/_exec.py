from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from uuid import uuid4

from sca.core.errors import SandboxError
from sca.core.models import ExecEvent, ExecExit, ExecOutput


async def stream_exec(
    command: Callable[[str, str], list[str]],
    cleanup_command: Callable[[str, str], Awaitable[object]],
    *,
    stdin: bytes | None = None,
    timeout_s: float | None = None,
) -> AsyncIterator[ExecEvent]:
    marker = f"/tmp/sca-exec-{uuid4().hex}"
    wrapper = (
        # A `docker exec` process is usually already a group leader, in which case
        # setsid is refused and its pid is the process group id we need.
        "import os,sys\n"
        "try: os.setsid()\n"
        "except OSError: pass\n"
        "open(sys.argv[1], 'w').write(str(os.getpgid(0)))\n"
        "os.execvp(sys.argv[2],sys.argv[2:])"
    )
    args = command(wrapper, marker)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise SandboxError(str(exc)) from exc

    async def cleanup(kill: bool) -> None:
        script = (
            "import os,sys,signal,time; p=sys.argv[1]; "
            + (
                "\nfor _ in range(50):\n if os.path.exists(p): break\n time.sleep(.01)\n"
                "try: os.killpg(int(open(p).read()),signal.SIGKILL)\n"
                "except (FileNotFoundError,ProcessLookupError): pass\n"
                if kill
                else ""
            )
            + "\ntry: os.unlink(p)\nexcept FileNotFoundError: pass\n"
        )
        await cleanup_command(script, marker)
        if kill and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()

    async with aclosing(stream_process(proc, cleanup, stdin=stdin, timeout_s=timeout_s)) as events:
        async for event in events:
            yield event


async def stream_process(
    proc: asyncio.subprocess.Process,
    cleanup: Callable[[bool], Awaitable[None]],
    *,
    stdin: bytes | None = None,
    timeout_s: float | None = None,
) -> AsyncIterator[ExecEvent]:
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    writer = proc.stdin
    queue: asyncio.Queue[ExecOutput | None] = asyncio.Queue()

    async def read(stream: asyncio.StreamReader, name: str) -> None:
        pending = b""
        try:
            while chunk := await stream.read(65536):
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    await queue.put(ExecOutput(stream=name, text=line.decode(errors="replace")))
            if pending:
                await queue.put(ExecOutput(stream=name, text=pending.decode(errors="replace")))
        finally:
            await queue.put(None)

    async def write() -> None:
        try:
            if stdin:
                writer.write(stdin)
                await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()

    tasks = [
        asyncio.create_task(read(proc.stdout, "stdout")),
        asyncio.create_task(read(proc.stderr, "stderr")),
        asyncio.create_task(write()),
    ]
    finished = False
    timed_out = False
    deadline = None if timeout_s is None else asyncio.get_running_loop().time() + timeout_s

    def remaining() -> float | None:
        return None if deadline is None else max(0, deadline - asyncio.get_running_loop().time())

    try:
        try:
            ends = 0
            while ends < 2:
                event = await asyncio.wait_for(queue.get(), remaining())
                if event is None:
                    ends += 1
                else:
                    yield event
            await asyncio.wait_for(proc.wait(), remaining())
        except TimeoutError:
            timed_out = True
            await cleanup(True)
        if not timed_out:
            await cleanup(False)
        finished = True
        yield ExecExit(exit_code=124 if timed_out else proc.returncode, timed_out=timed_out)
    finally:
        if not finished:
            await cleanup(True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
