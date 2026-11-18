"""Stdio-only NDJSON executor. Payload bytes are base64 encoded."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sys

MAX_FRAME = 16 * 1024 * 1024


async def main() -> None:
    reader = asyncio.StreamReader(limit=MAX_FRAME)
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    jobs: dict[str, asyncio.Task] = {}
    processes: dict[str, asyncio.subprocess.Process] = {}
    cancelled: set[str] = set()

    def send(message: dict) -> None:
        sys.stdout.buffer.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        sys.stdout.buffer.flush()

    def kill(ident: str) -> None:
        cancelled.add(ident)
        proc = processes.get(ident)
        if proc:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def execute(request: dict) -> None:
        ident = request["id"]
        proc = None
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_exec(
                *request["argv"], cwd=request.get("cwd"),
                env={**os.environ, **request.get("env", {})}, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            processes[ident] = proc
            if ident in cancelled:
                kill(ident)

            async def read(stream, name: str) -> None:
                while chunk := await stream.read(32768):
                    send({"id": ident, name: base64.b64encode(chunk).decode()})

            async def write() -> None:
                try:
                    proc.stdin.write(base64.b64decode(request.get("stdin", ""), validate=True))
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()

            tasks = [asyncio.create_task(read(proc.stdout, "stdout")),
                     asyncio.create_task(read(proc.stderr, "stderr")),
                     asyncio.create_task(write())]
            try:
                async with asyncio.timeout(request.get("timeout")):
                    await asyncio.gather(*tasks, proc.wait())
            except TimeoutError:
                timed_out = True
                kill(ident)
                await asyncio.gather(*tasks, proc.wait(), return_exceptions=True)
            finally:
                kill(ident)
                await proc.wait()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            send({"id": ident, "exit": 124 if timed_out else proc.returncode,
                  "timed_out": timed_out})
        except (OSError, ValueError, TypeError):
            send({"id": ident, "exit": 127, "timed_out": False})
        finally:
            processes.pop(ident, None)
            jobs.pop(ident, None)
            cancelled.discard(ident)

    try:
        while line := await reader.readline():
            request = json.loads(line)
            ident = request["id"]
            if request.get("ping"):
                send({"id": ident, "pong": True})
            elif request.get("cancel"):
                kill(ident)
            elif ident not in jobs:
                jobs[ident] = asyncio.create_task(execute(request))
    finally:
        for ident in list(jobs):
            kill(ident)
        await asyncio.gather(*jobs.values(), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
