from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import weakref
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from sca.agents.opencode import parse_opencode_line, validate_version
from sca.config import Settings, get_settings
from sca.core.errors import AgentTurnError
from sca.core.interfaces import SandboxProvider, unpause_sandbox
from sca.core.models import AgentContext, AgentEvent, SandboxAdditions


def link_dependencies(source: Path, cache: Path) -> None:
    packages = {}
    for manifest in (source / "packages").glob("*/package.json"):
        name = json.loads(manifest.read_text()).get("name", "")
        if name.startswith("@opencode/"):
            packages[name] = manifest.parent
    for cached in [cache / "node_modules", *cache.glob("packages/*/node_modules")]:
        if not cached.is_dir():
            continue
        modules = source / cached.relative_to(cache)
        modules.mkdir(parents=True, exist_ok=True)
        for entry in cached.iterdir():
            if entry.name == "@opencode":
                continue
            (modules / entry.name).symlink_to(entry.resolve())
        scope = modules / "@opencode"
        scope.mkdir(exist_ok=True)
        for name, target in packages.items():
            # Relative links survive the atomic staging-directory rename.
            (scope / name.split("/")[1]).symlink_to(os.path.relpath(target, scope))
        # Published @opencode/* packages (e.g. @opencode/pty) are third-party deps, not
        # workspace packages: they still come read-only from the cache.
        cached_scope = cached / "@opencode"
        if cached_scope.is_dir():
            for entry in cached_scope.iterdir():
                if not (scope / entry.name).exists() and not (scope / entry.name).is_symlink():
                    resolved = entry.resolve()
                    if "node_modules" in resolved.parts:
                        (scope / entry.name).symlink_to(resolved)


@dataclass
class BrainServer:
    directory: Path
    semaphore: asyncio.Semaphore
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    process: asyncio.subprocess.Process | None = None
    idle: asyncio.Task[None] | None = None
    active: int = 0


class BrainPool:
    instances: ClassVar[weakref.WeakSet[BrainPool]] = weakref.WeakSet()

    def __init__(self, settings: Settings, command: list[str]) -> None:
        self.settings, self.command = settings, command
        self.servers: dict[str, BrainServer] = {}
        self.closed = False
        self.worker_id = settings.opencode_brain_pool_worker_id
        self.instances.add(self)

    async def connect(self, server: BrainServer) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # Linux unix sockets have a 108-byte path limit, including long data_dir prefixes.
        descriptor = os.open(server.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return await asyncio.open_unix_connection(
                f"/proc/self/fd/{descriptor}/brain.sock", limit=4 * 1024 * 1024)
        finally:
            os.close(descriptor)

    async def ping(self, server: BrainServer) -> dict[str, object]:
        async with asyncio.timeout(2):
            reader, writer = await self.connect(server)
            try:
                writer.write(b'{"op":"ping","id":"health"}\n')
                await writer.drain()
                reply = json.loads(await reader.readline())
                if not isinstance(reply, dict) or reply.get("pong") is not True or reply.get("id") != "health":
                    raise AgentTurnError("Invalid brain health response")
                return reply
            finally:
                writer.close()
                await writer.wait_closed()

    async def _stop(self, server: BrainServer) -> None:
        process, server.process = server.process, None
        if process is not None:
            # Kill the group even if the launcher died before its Bun child.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    async def _start(self, server: BrainServer, source: Path) -> bool:
        if self.closed:
            raise AgentTurnError("Brain pool is closed")
        if server.process is not None and server.process.returncode is None:
            try:
                await self.ping(server)
                return False
            except (OSError, TimeoutError, ValueError, AgentTurnError):
                await self._stop(server)
        else:
            await self._stop(server)
        server.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        server.directory.chmod(0o700)
        command = list(self.command)
        command[2] = str(source.resolve())
        generation = uuid.uuid4().hex
        server.process = await asyncio.create_subprocess_exec(
            *command, "--server", str(server.directory), generation,
            cwd=server.directory,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(server.directory)},
        )
        try:
            async with asyncio.timeout(90):
                while server.process.returncode is None:
                    try:
                        reply = await self.ping(server)
                        if reply.get("generation") == generation:
                            return True
                        await asyncio.sleep(0.05)
                    except (OSError, TimeoutError, ValueError):
                        await asyncio.sleep(0.05)
                raise AgentTurnError("Brain server failed to start")
        except BaseException:
            await self._stop(server)
            raise

    async def _expire(self, server: BrainServer) -> None:
        await asyncio.sleep(self.settings.opencode_brain_pool_idle_minutes * 60)
        async with server.lock:
            if not server.active:
                await self._stop(server)

    async def stream(self, version: str, source: Path,
                     request: dict[str, object]) -> AsyncIterator[dict[str, object]]:
        if self.closed:
            raise AgentTurnError("Brain pool is closed")
        version = validate_version(version)
        server = self.servers.setdefault(version, BrainServer(
            self.settings.data_dir.resolve() / "opencode/brain-run" / version / self.worker_id,
            asyncio.Semaphore(self.settings.opencode_brain_pool_concurrency)))
        async with server.semaphore:
            writer: asyncio.StreamWriter | None = None
            async with server.lock:
                if server.idle:
                    server.idle.cancel()
                    server.idle = None
                cold = await self._start(server, source)
                server.active += 1
            request_id = uuid.uuid4().hex
            done = False
            try:
                reader, writer = await self.connect(server)
                writer.write((json.dumps({**request, "op": "turn", "id": request_id}) + "\n").encode())
                await writer.drain()
                yield {"pool": "cold" if cold else "warm"}
                while line := await reader.readline():
                    raw = json.loads(line)
                    if not isinstance(raw, dict) or raw.get("id") != request_id:
                        raise AgentTurnError("Invalid brain protocol response")
                    if raw.get("done") is True:
                        done = True
                        if raw.get("exit_code") != 0:
                            raise AgentTurnError("Brain server turn failed")
                        return
                    yield raw
                raise AgentTurnError("Brain server disconnected during turn")
            except (OSError, ValueError) as exc:
                raise AgentTurnError("Brain protocol connection failed") from exc
            finally:
                if writer is not None:
                    if not done and not writer.is_closing():
                        writer.write((json.dumps({"op": "cancel", "id": request_id}) + "\n").encode())
                        try:
                            await asyncio.wait_for(writer.drain(), 1)
                            if request.get("suspend_idle") is True:
                                # Do not let a late brain timer freeze kernel cleanup.
                                async with asyncio.timeout(25):
                                    while line := await reader.readline():
                                        reply = json.loads(line)
                                        if reply.get("id") == request_id and reply.get("done") is True:
                                            break
                                    else:
                                        await self._stop(server)
                        except (OSError, TimeoutError, ValueError):
                            if request.get("suspend_idle") is True:
                                await self._stop(server)
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
                async with server.lock:
                    server.active -= 1
                    if not server.active and not self.closed:
                        server.idle = asyncio.create_task(self._expire(server))

    async def close(self) -> None:
        self.closed = True
        for server in self.servers.values():
            if server.idle:
                server.idle.cancel()
                await asyncio.gather(server.idle, return_exceptions=True)
            async with server.lock:
                await self._stop(server)

    @classmethod
    async def close_all(cls) -> None:
        await asyncio.gather(*(pool.close() for pool in list(cls.instances)))


class OpenCodeBrainBackend:
    name = "opencode-brain"
    state_transport = "mount"
    sandbox_network = "none"

    def __init__(
        self, source: Path, bun: Path, host: Path, default_model: str,
        gateway_url: str | None = None, docker_prefix: list[str] | None = None,
        *, settings: Settings | None = None,
    ) -> None:
        self.source, self.bun, self.host = source.resolve(), bun.resolve(), host.resolve()
        self.settings = settings or get_settings()
        self.pool = BrainPool(self.settings, self.command())
        self.default_model = default_model
        self.gateway_url = gateway_url or self.settings.opencode_brain_gateway_url
        self.docker_prefix = docker_prefix or ["docker"]
        if not self.source.is_dir() or not self.bun.is_file() or not self.host.is_file():
            raise FileNotFoundError("OpenCode brain source, Bun or launcher is missing")

    def sandbox_additions(self, ctx: AgentContext) -> SandboxAdditions:
        return SandboxAdditions()

    def request(self, sandbox_id: str, ctx: AgentContext, prompt: str,
                session_id: str | None, marker: str = "") -> dict[str, object]:
        return {
            "prompt": prompt, "model": ctx.model or self.default_model,
            "gateway_url": self.gateway_url,
            "container": sandbox_id, "workdir": ctx.workdir,
            "session_dir": str(Path(ctx.agent_home_host).resolve()),
            "task_id": ctx.task_id, "session_id": session_id,
            "docker_prefix": self.docker_prefix,
            "turn_marker": marker,
            "suspend_idle": self.settings.sandbox_suspend_idle,
            "suspend_idle_min_s": self.settings.sandbox_suspend_idle_min_s,
        }

    def materialize(self, version: str | None) -> Path:
        settings = self.settings
        version = validate_version(version or settings.opencode_default_version)
        versions = settings.opencode_versions_dir
        assert versions is not None
        meta = json.loads((versions / version / "meta.json").read_text())
        commit = meta.get("commit", "")
        if (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)
                or meta.get("source_repo") != settings.opencode_repo_url):
            raise AgentTurnError("Invalid brain version metadata")
        root = settings.data_dir.resolve() / "opencode/brain-src"
        root.mkdir(parents=True, exist_ok=True)
        destination = root / version
        with (root / "source.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for old in root.glob("2.3.0-sca.*"):
                if old.is_dir() and not (versions / old.name / "meta.json").is_file():
                    shutil.rmtree(old)
            marker = destination / ".sca-commit"
            if destination.exists():
                if destination.is_symlink() or marker.read_text() != commit:
                    raise AgentTurnError("Brain source commit marker mismatch")
                return destination
            with tempfile.TemporaryDirectory(dir=root, prefix="archive-") as temporary:
                staging = Path(temporary) / "source"
                staging.mkdir()
                archive = Path(temporary) / "source.tar"
                # Only the configured trusted mirror is accessed, never an agent checkout.
                subprocess.run(["git", "--git-dir", str(settings.opencode_repo_url),
                                "archive", "--format=tar", "--output", str(archive), commit],
                               check=True, timeout=120, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
                with tarfile.open(archive) as stream:
                    stream.extractall(staging, filter="data")
                link_dependencies(staging, self.source)
                (staging / ".sca-commit").write_text(commit)
                staging.rename(destination)
        return destination

    def cleanup_command(self, container: str, marker: str) -> list[str]:
        script = (
            "import glob,os,signal,sys\n"
            "open('/tmp/sca-brain-' + sys.argv[1] + '.cancelled', 'w').close()\n"
            "for path in glob.glob('/tmp/sca-brain-' + sys.argv[1] + '-*'):\n"
            " try: os.killpg(int(open(path).read()), signal.SIGKILL)\n"
            " except (FileNotFoundError,ProcessLookupError): pass\n"
            " try: os.unlink(path)\n"
            " except FileNotFoundError: pass\n"
        )
        args = ["exec", container, "python3", "-c", script, marker]
        if self.docker_prefix[0] == "sg":
            import shlex
            return [*self.docker_prefix, shlex.join(["docker", *args])]
        return [*self.docker_prefix, *args]

    def command(self) -> list[str]:
        return [sys.executable, str(self.host), str(self.source), str(self.bun)]

    def environment(self, ctx: AgentContext) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(Path(ctx.agent_home_host).resolve()),
                "SCA_MODEL_KEY": ctx.model_api_key or ""}

    async def run_turn(
        self, sandbox: SandboxProvider, sandbox_id: str, ctx: AgentContext, prompt: str,
        *, session_id: str | None, timeout_s: float,
    ) -> AsyncIterator[AgentEvent]:
        if not ctx.model_api_key or not (self.gateway_url):
            raise AgentTurnError("OpenCode brain requires a model gateway and per-turn key")
        source = await asyncio.to_thread(self.materialize, ctx.agent_version)
        marker = uuid.uuid4().hex
        command = self.command()
        if len(command) >= 4:
            command[2] = str(source.resolve())
        Path(ctx.agent_home_host).mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        model_ms = tool_ms = 0
        pooled = self.settings.opencode_brain_pool
        pool_status = "cold"
        process = None

        async def records() -> AsyncIterator[dict[str, object]]:
            nonlocal process, pool_status
            request = self.request(sandbox_id, ctx, prompt, session_id, marker)
            if pooled:
                stream = self.pool.stream(
                    ctx.agent_version or self.settings.opencode_default_version, source,
                    {**request, "model_key": ctx.model_api_key})
                try:
                    async for raw in stream:
                        if "pool" in raw:
                            pool_status = str(raw["pool"])
                        else:
                            yield raw
                finally:
                    await stream.aclose()
                return
            # Legacy process stderr is discarded: upstream diagnostics may contain credentials.
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=self.environment(ctx),
                start_new_session=True, limit=4 * 1024 * 1024,
            )
            assert process.stdin and process.stdout
            process.stdin.write(json.dumps(request).encode())
            await process.stdin.drain()
            process.stdin.close()
            async for line in process.stdout:
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if isinstance(raw, dict):
                    yield raw
            if await process.wait():
                raise AgentTurnError("OpenCode brain process failed")

        final, finished, failed, seen = "", False, False, session_id
        steps: dict[str, dict[str, int]] = {}
        try:
            async with asyncio.timeout(timeout_s):
                stream = records()
                try:
                    async for raw in stream:
                        if raw.get("sca_suspend_transition") is True:
                            at = raw.get("at")
                            if isinstance(raw.get("paused"), bool) and isinstance(at, (int, float)):
                                yield AgentEvent(type="usage", data={"summary": "sandbox_transition",
                                    "paused": raw["paused"], "at": at})
                            continue
                        if raw.get("sca_suspend") is True:
                            value = raw.get("suspended_s", 0)
                            if isinstance(value, (int, float)) and 0 <= value <= timeout_s:
                                yield AgentEvent(type="usage", data={
                                    "summary": "sandbox_suspend", "sandbox_suspended_s": value})
                            continue
                        text = json.dumps(raw).replace(json.dumps(ctx.model_api_key)[1:-1], "[redacted]")
                        raw = json.loads(text)
                        if not seen and isinstance(raw.get("sessionID"), str):
                            seen = raw["sessionID"]
                            yield AgentEvent(type="session_started", data={"session_id": seen})
                        for event in parse_opencode_line(text, steps):
                            if event.type == "message":
                                final += event.data["text"]
                            if event.type == "usage":
                                model_ms += event.data.get("model_call_duration_ms", 0)
                            elif event.type in {"command", "tool", "file_change"}:
                                tool_ms += event.data.get("duration_ms", 0)
                            failed |= event.type == "error"
                            if event.type == "usage" and (raw.get("part") or {}).get("reason") == "stop":
                                finished = True
                            yield event
                finally:
                    await stream.aclose()
                if failed or not finished:
                    raise AgentTurnError("OpenCode brain ended without a completed step")
        except TimeoutError as exc:
            raise AgentTurnError("OpenCode brain turn timed out") from exc
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            if sandbox is not None:
                await unpause_sandbox(sandbox, sandbox_id)
            cleanup = await asyncio.create_subprocess_exec(
                *self.cleanup_command(sandbox_id, marker),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(cleanup.wait(), 10)
            except TimeoutError:
                cleanup.kill()
                await cleanup.wait()
        yield AgentEvent(type="usage", data={"summary": "brain_turn", "pool": pool_status,
            "brain_wall_time_ms": round((time.monotonic() - started) * 1000),
            "total_model_call_ms": model_ms, "total_tool_ms": tool_ms})
        yield AgentEvent(type="turn_completed", data={"final_message": final})
