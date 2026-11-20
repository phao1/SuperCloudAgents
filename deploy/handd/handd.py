"""Stdio-only NDJSON executor. Payload bytes are base64 encoded."""
from __future__ import annotations

import asyncio
import base64
import errno
import json
import os
import pty
import signal
import sys
import termios
from contextlib import suppress
from dataclasses import dataclass

MAX_FRAME = 16 * 1024 * 1024
CHUNK = 32 * 1024
MAX_PTY_OUTPUT = 1024 * 1024
PTY_CHILD = (
    "import fcntl,os,sys,termios\n"
    "try:\n"
    " fcntl.ioctl(0,termios.TIOCSCTTY,0)\n"
    " os.execvp(sys.argv[1],sys.argv[1:])\n"
    "except OSError:\n"
    " sys.exit(127)"
)


def send(message: dict) -> None:
    sys.stdout.buffer.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
    sys.stdout.buffer.flush()


async def fd_ready(fd: int, *, write: bool = False) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    register = loop.add_writer if write else loop.add_reader
    remove = loop.remove_writer if write else loop.remove_reader
    register(fd, lambda: not ready.done() and ready.set_result(None))
    try:
        await ready
    finally:
        remove(fd)


def resize(fd: int, cols: int, rows: int) -> None:
    if any(type(value) is not int or not 1 <= value <= 65535 for value in (cols, rows)):
        raise ValueError("invalid PTY size")
    termios.tcsetwinsize(fd, (rows, cols))


@dataclass
class Job:
    process: asyncio.subprocess.Process | None = None
    master: int | None = None
    inputs: asyncio.Queue[bytes] | None = None
    cancelled: bool = False
    truncated: bool = False

    def kill(self) -> None:
        self.cancelled = True
        if self.master is not None:
            # Interactive shells can put the current job in its own foreground group.
            with suppress(OSError):
                foreground = os.tcgetpgrp(self.master)
                if foreground > 0:
                    os.killpg(foreground, signal.SIGKILL)
        if self.process is not None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)


async def execute(request: dict, job: Job) -> None:
    ident = request["id"]
    if job.cancelled:
        send({"id": ident, "exit": -9, "timed_out": False})
        return
    timed_out = False
    code = 127
    tasks: list[asyncio.Task[None]] = []
    try:
        argv = request["argv"]
        env = {**os.environ, **request.get("env", {})}
        options = {
            "cwd": request.get("cwd"), "env": env, "start_new_session": True,
        }
        if request.get("op") == "pty.open":
            env.setdefault("TERM", "xterm-256color")
            job.master, slave = pty.openpty()
            try:
                os.set_blocking(job.master, False)
                resize(slave, request["cols"], request["rows"])
                job.process = await asyncio.create_subprocess_exec(
                    sys.executable, "-S", "-c", PTY_CHILD, *argv,
                    stdin=slave, stdout=slave, stderr=slave, **options,
                )
            finally:
                os.close(slave)
            job.inputs = asyncio.Queue(maxsize=MAX_FRAME // CHUNK)
        else:
            job.process = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, **options,
            )
        if job.cancelled:
            job.kill()

        if job.master is not None:
            send({"id": ident, "opened": True})
            readers = [asyncio.create_task(read_pty(ident, job))]
            tasks = [*readers, asyncio.create_task(write_pty(job))]
        else:
            assert job.process.stdout and job.process.stderr and job.process.stdin
            tasks = readers = [
                asyncio.create_task(read_pipe(ident, job.process.stdout, "stdout")),
                asyncio.create_task(read_pipe(ident, job.process.stderr, "stderr")),
                asyncio.create_task(write_pipe(job.process.stdin, request.get("stdin", ""))),
            ]
        async with asyncio.timeout(request.get("timeout")):
            await asyncio.gather(*readers, job.process.wait())
        code = job.process.returncode
    except TimeoutError:
        timed_out, code = True, 124
    except (OSError, ValueError, TypeError, KeyError):
        pass
    finally:
        job.kill()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if job.master is not None:
            os.close(job.master)
            job.master = None
        if job.process is not None:
            await job.process.wait()
    reply = {"id": ident, "exit": code, "timed_out": timed_out}
    if job.truncated:
        reply["truncated"] = True
    send(reply)


def output(ident: str, stream: str, data: bytes) -> None:
    send({"id": ident, stream: base64.b64encode(data).decode()})


async def read_pipe(ident: str, stream: asyncio.StreamReader, name: str) -> None:
    while chunk := await stream.read(CHUNK):
        output(ident, name, chunk)


async def write_pipe(stream: asyncio.StreamWriter, data: str) -> None:
    try:
        stream.write(base64.b64decode(data, validate=True))
        await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stream.close()


async def read_pty(ident: str, job: Job) -> None:
    assert job.master is not None
    remaining = MAX_PTY_OUTPUT
    while True:
        await fd_ready(job.master)
        try:
            chunk = os.read(job.master, CHUNK)
        except BlockingIOError:
            continue
        except OSError as exc:
            if exc.errno == errno.EIO:  # The last slave descriptor was closed.
                return
            raise
        if not chunk:
            return
        if remaining:
            output(ident, "stdout", chunk[:remaining])
        job.truncated |= len(chunk) > remaining
        remaining = max(0, remaining - len(chunk))


async def write_pty(job: Job) -> None:
    assert job.master is not None and job.inputs is not None
    while True:
        data = memoryview(await job.inputs.get())
        while data:
            try:
                data = data[os.write(job.master, data):]
            except BlockingIOError:
                await fd_ready(job.master, write=True)
            except OSError:
                return


def control(job: Job, request: dict) -> None:
    operation = request.get("op")
    if request.get("cancel") or operation == "pty.close":
        job.kill()
    elif job.master is not None and job.inputs is not None:
        try:
            if operation == "pty.resize":
                resize(job.master, request["cols"], request["rows"])
            elif operation == "pty.write":
                data = base64.b64decode(request["data"], validate=True)
                if len(data) > CHUNK:
                    raise ValueError("PTY input frame exceeds limit")
                job.inputs.put_nowait(data)
        except (OSError, ValueError, TypeError, KeyError, asyncio.QueueFull):
            job.kill()


async def main() -> None:
    reader = asyncio.StreamReader(limit=MAX_FRAME)
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    jobs: dict[str, Job] = {}
    tasks: dict[str, asyncio.Task[None]] = {}

    async def run(request: dict, job: Job) -> None:
        try:
            await execute(request, job)
        finally:
            jobs.pop(request["id"])
            tasks.pop(request["id"])

    try:
        while line := await reader.readline():
            request = json.loads(line)
            ident = request["id"]
            if request.get("ping"):
                send({"id": ident, "pong": True, "pty": True})
            elif ident in jobs:
                control(jobs[ident], request)
            elif "argv" in request:
                job = jobs[ident] = Job()
                tasks[ident] = asyncio.create_task(run(request, job))
    finally:
        for job in jobs.values():
            job.kill()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
