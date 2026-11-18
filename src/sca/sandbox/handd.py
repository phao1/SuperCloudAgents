from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from sca.core.errors import SandboxError
from sca.core.models import ExecEvent, ExecExit, ExecOutput

DAEMON = Path(__file__).resolve().parents[3] / "deploy/handd/handd.py"
MAX_FRAME = 16 * 1024 * 1024


class HanddClient:
    def __init__(self, command: list[str]):
        self.command = command
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task | None = None
        self.pending: dict[str, asyncio.Queue] = {}
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        async with self.lock:
            if self.reader and not self.reader.done():
                return
            await self.close()
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.command, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    limit=MAX_FRAME,
                )
                self.reader = asyncio.create_task(self._read())
                ident = uuid4().hex
                queue = self.pending[ident] = asyncio.Queue()
                try:
                    await self._send({"id": ident, "ping": True})
                    reply = await asyncio.wait_for(queue.get(), 30)
                    if isinstance(reply, Exception) or not reply.get("pong"):
                        raise SandboxError("handd unavailable")
                finally:
                    self.pending.pop(ident, None)
            except (OSError, TimeoutError, SandboxError) as exc:
                await self.close()
                raise SandboxError("handd unavailable") from exc

    async def _send(self, message: dict) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        if len(payload) > MAX_FRAME:
            raise SandboxError("handd request exceeds frame limit")
        try:
            self.proc.stdin.write(payload)
            await self.proc.stdin.drain()
        except (AttributeError, BrokenPipeError, ConnectionResetError) as exc:
            raise SandboxError("handd disconnected") from exc

    async def _read(self) -> None:
        try:
            while line := await self.proc.stdout.readline():
                message = json.loads(line)
                queue = self.pending.get(message["id"])
                if queue is not None:
                    queue.put_nowait(message)
        except (ValueError, KeyError, TypeError):
            pass
        finally:
            for queue in self.pending.values():
                queue.put_nowait(SandboxError("handd disconnected; command outcome unknown"))

    async def close(self) -> None:
        if self.proc:
            if self.proc.returncode is None:
                self.proc.kill()
            await self.proc.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        self.proc = None
        self.reader = None

    async def exec(
        self, argv: list[str], *, workdir: str | None = None,
        env: dict[str, str] | None = None, stdin: bytes | None = None,
        timeout_s: float | None = None,
    ) -> AsyncIterator[ExecEvent]:
        ident = uuid4().hex
        queue = self.pending[ident] = asyncio.Queue()
        pending = {"stdout": b"", "stderr": b""}
        finished = False
        try:
            await self._send({"id": ident, "argv": argv, "cwd": workdir, "env": env or {},
                              "stdin": base64.b64encode(stdin or b"").decode(),
                              "timeout": timeout_s})
            while True:
                message = await queue.get()
                if isinstance(message, Exception):
                    raise message
                if "exit" in message:
                    for name, data in pending.items():
                        if data:
                            yield ExecOutput(stream=name, text=data.decode(errors="replace"))
                    finished = True
                    yield ExecExit(exit_code=message["exit"], timed_out=message["timed_out"])
                    break
                for name, data in pending.items():
                    if name in message:
                        pending[name] = data + base64.b64decode(message[name], validate=True)
                        while b"\n" in pending[name]:
                            line, pending[name] = pending[name].split(b"\n", 1)
                            yield ExecOutput(stream=name, text=line.decode(errors="replace"))
        finally:
            if not finished:
                try:
                    await self._send({"id": ident, "cancel": True})
                    async with asyncio.timeout(30):
                        while True:
                            reply = await queue.get()
                            if isinstance(reply, Exception) or "exit" in reply:
                                break
                except (SandboxError, TimeoutError):
                    await self.close()
            self.pending.pop(ident, None)
