from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from sca.core.errors import SandboxError
from sca.core.models import ExecEvent, ExecExit, ExecOutput

DAEMON = Path(__file__).with_name("_handd_daemon.py")
if not DAEMON.is_file():
    DAEMON = Path(__file__).resolve().parents[3] / "deploy/handd/handd.py"
MAX_FRAME = 16 * 1024 * 1024
CHUNK = 32 * 1024
MAX_PTY_OUTPUT = 1024 * 1024

Frame = dict[str, Any]


def terminal_socket(data_dir: Path, task_id: str) -> Path:
    name = hashlib.sha256(task_id.encode()).hexdigest()
    return data_dir.resolve() / "opencode" / "handd" / f"{name}.sock"


@contextmanager
def unix_address(path: Path) -> Iterator[str]:
    # Linux limits the address, regardless of the allowed filesystem path length.
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield f"/proc/self/fd/{fd}/{path.name}"
    finally:
        os.close(fd)


class HanddProcess:
    def __init__(self, client: HanddClient, *, pty: bool = False):
        self.client = client
        self.process = client.proc
        self.id = uuid4().hex
        self.pty = pty
        self.queue: asyncio.Queue[Frame | SandboxError] = asyncio.Queue()
        self.finished = asyncio.Event()
        self.remaining = MAX_PTY_OUTPUT
        self.truncated = False

    def feed(self, frame: Frame | SandboxError) -> None:
        if self.finished.is_set():
            return
        if self.pty and isinstance(frame, dict):
            for stream in ("stdout", "stderr"):
                if stream in frame:
                    data = base64.b64decode(frame[stream], validate=True)
                    if len(data) > self.remaining:
                        self.truncated = True
                        data = data[:self.remaining]
                        frame[stream] = base64.b64encode(data).decode()
                    self.remaining -= len(data)
            if "exit" in frame and self.truncated:
                frame["truncated"] = True
            if set(frame) <= {"id", "stdout", "stderr"} and not (
                frame.get("stdout") or frame.get("stderr")
            ):
                return
        self.queue.put_nowait(frame)
        if isinstance(frame, SandboxError) or "exit" in frame:
            self.finished.set()

    async def receive(self) -> Frame:
        frame = await self.queue.get()
        if isinstance(frame, SandboxError):
            raise frame
        return frame

    async def frames(self) -> AsyncIterator[Frame]:
        while True:
            frame = await self.receive()
            yield frame
            if "exit" in frame:
                return

    async def write(self, data: bytes) -> None:
        if not self.pty:
            raise SandboxError("interactive stdin requires a PTY")
        for offset in range(0, len(data), CHUNK):
            await self.send({"op": "pty.write",
                             "data": base64.b64encode(data[offset:offset + CHUNK]).decode()})

    async def resize(self, cols: int, rows: int) -> None:
        if not self.pty:
            raise SandboxError("resize requires a PTY")
        await self.send({"op": "pty.resize", "cols": cols, "rows": rows})

    async def send(self, frame: Frame) -> None:
        if self.client.proc is not self.process:
            raise SandboxError("handd connection replaced")
        await self.client._send({"id": self.id, **frame})

    async def close(self) -> None:
        if self.finished.is_set():
            return
        message = {"op": "pty.close"} if self.pty else {"cancel": True}
        try:
            await self.send(message)
            await asyncio.wait_for(self.finished.wait(), 30)
        except (SandboxError, TimeoutError):
            if self.client.proc is self.process:
                await self.client.close()


class HanddClient:
    def __init__(self, command: list[str], *, environment: dict[str, str] | None = None):
        self.command = command
        self.environment = environment
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.pending: dict[str, HanddProcess] = {}
        self.lock = asyncio.Lock()
        self.supports_pty = False

    async def start(self) -> None:
        async with self.lock:
            if self.reader and not self.reader.done():
                return
            await self.close()
            try:
                proc = self.proc = await asyncio.create_subprocess_exec(
                    *self.command, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    env=self.environment, limit=MAX_FRAME,
                )
                self.reader = asyncio.create_task(self._read(proc))
                async with asyncio.timeout(30):
                    health = HanddProcess(self)
                    self.pending[health.id] = health
                    try:
                        await health.send({"ping": True})
                        reply = await health.receive()
                    finally:
                        self.pending.pop(health.id, None)
                    if reply.get("pong") is not True:
                        raise SandboxError("handd unavailable")
                    self.supports_pty = reply.get("pty") is True
            except (OSError, TimeoutError, SandboxError) as exc:
                await self.close()
                raise SandboxError("handd unavailable") from exc
            except asyncio.CancelledError:
                await self.close()
                raise

    async def _send(self, message: Frame) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        if len(payload) > MAX_FRAME:
            raise SandboxError("handd request exceeds frame limit")
        if self.proc is None or self.proc.stdin is None or self.proc.stdin.is_closing():
            raise SandboxError("handd disconnected")
        try:
            self.proc.stdin.write(payload)
            await self.proc.stdin.drain()
        except (OSError, RuntimeError) as exc:
            raise SandboxError("handd disconnected") from exc

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while line := await proc.stdout.readline():
                message = json.loads(line)
                job = self.pending.get(message["id"])
                if job is not None and job.process is proc:
                    job.feed(message)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
            for job in self.pending.values():
                if job.process is proc:
                    job.feed(SandboxError("handd disconnected; command outcome unknown"))

    async def close(self) -> None:
        proc, reader = self.proc, self.reader
        self.proc, self.reader, self.supports_pty = None, None, False
        if proc is not None:
            assert proc.stdin is not None
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    @asynccontextmanager
    async def _open(self, request: Frame, *, pty: bool = False) -> AsyncIterator[HanddProcess]:
        job = HanddProcess(self, pty=pty)
        self.pending[job.id] = job
        try:
            await job.send(request)
            if pty:
                reply = await job.receive()
                if "exit" in reply:
                    job.queue.put_nowait(reply)
                elif reply.get("opened") is not True:
                    raise SandboxError("invalid handd PTY response")
            yield job
        finally:
            try:
                await job.close()
            finally:
                self.pending.pop(job.id, None)

    def open_exec(
        self, argv: list[str], *, workdir: str | None = None,
        env: dict[str, str] | None = None, stdin: bytes | None = None,
        timeout_s: float | None = None,
    ) -> AbstractAsyncContextManager[HanddProcess]:
        return self._open({"argv": argv, "cwd": workdir, "env": env or {},
                           "stdin": base64.b64encode(stdin or b"").decode(),
                           "timeout": timeout_s})

    def open_pty(
        self, argv: list[str], *, workdir: str | None = None, env: dict[str, str] | None = None,
        cols: int = 80, rows: int = 24, timeout_s: float | None = None,
    ) -> AbstractAsyncContextManager[HanddProcess]:
        return self._open({"op": "pty.open", "argv": argv, "cwd": workdir, "env": env or {},
                           "cols": cols, "rows": rows, "timeout": timeout_s}, pty=True)

    async def exec(
        self, argv: list[str], *, workdir: str | None = None,
        env: dict[str, str] | None = None, stdin: bytes | None = None,
        timeout_s: float | None = None,
    ) -> AsyncIterator[ExecEvent]:
        pending = {"stdout": b"", "stderr": b""}
        async with self.open_exec(
            argv, workdir=workdir, env=env, stdin=stdin, timeout_s=timeout_s,
        ) as job:
            async for message in job.frames():
                if "exit" in message:
                    for name, data in pending.items():
                        if data:
                            yield ExecOutput(stream=name, text=data.decode(errors="replace"))
                    yield ExecExit(exit_code=message["exit"], timed_out=message["timed_out"])
                    break
                for name, data in pending.items():
                    if name in message:
                        pending[name] = data + base64.b64decode(message[name], validate=True)
                        while b"\n" in pending[name]:
                            line, pending[name] = pending[name].split(b"\n", 1)
                            yield ExecOutput(stream=name, text=line.decode(errors="replace"))
