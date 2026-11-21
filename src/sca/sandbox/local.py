"""Per-task directories and subprocesses for trusted, local source-mode tasks."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import sys
import tarfile
import tempfile
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from pathlib import Path

from sca.core.errors import SandboxError
from sca.core.models import ExecEvent, SandboxSpec
from sca.sandbox._exec import stream_process, wait_for_spawn
from sca.workspace.tarsafe import chunks, extract, pack, receive

_VIRTUAL_DIRS = {"workspace", "agent_home", "tmp"}
_METADATA = ".sandbox.json"


def _path(root: Path, value: str) -> Path:
    path = root / value.lstrip("/")
    if not path.resolve().is_relative_to(root):
        raise SandboxError("Path escapes the local sandbox directory")
    return path


def _argument(root: Path, value: str) -> str:
    if value.startswith("/") and value.split("/", 2)[1] in _VIRTUAL_DIRS:
        return str(_path(root, value))
    return value


def _command(root: Path, argv: list[str]) -> list[str]:
    index = 0
    if argv[0] == "env":
        index = next((i for i, arg in enumerate(argv[1:], 1) if "=" not in arg), 0)
    if argv[index:index + 2] == ["bash", "-lc"]:
        # The kernel uses login shells in containers. A host login shell can
        # source user/system profiles and reset the task's PATH and HOME.
        return [*argv[:index], "bash", "--noprofile", "--norc", "-c", *argv[index + 2:]]
    end = argv.index("--", 1) if "--" in argv[1:] else len(argv)
    return [argv[0], *(_argument(root, arg) for arg in argv[1:end]), *argv[end:]]


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


class LocalSandboxProvider:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._processes: dict[str, set[asyncio.subprocess.Process]] = {}
        self._host_path = [
            entry for entry in os.get_exec_path() if Path(entry).is_absolute()
        ]

    def _directory(self, sandbox_id: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", sandbox_id):
            raise SandboxError("Invalid local sandbox name")
        directory = self.root / sandbox_id
        if directory.is_symlink():
            raise SandboxError("Invalid local sandbox directory")
        return directory

    def _spec(self, sandbox_id: str) -> SandboxSpec:
        metadata = self._directory(sandbox_id) / _METADATA
        if not metadata.is_file() or metadata.is_symlink():
            raise SandboxError("Local sandbox does not exist")
        return SandboxSpec.model_validate_json(metadata.read_bytes())

    async def create(self, name: str, spec: SandboxSpec) -> str:
        directory = self._directory(name)
        if await self.exists(name):
            return name
        if spec.mounts:
            raise SandboxError("The local sandbox uses sync state transport")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=".creating-", dir=self.root))
        try:
            for child in _VIRTUAL_DIRS | {"bin"}:
                (staged / child).mkdir()
            (staged / _METADATA).write_text(spec.model_dump_json(), encoding="utf-8")
            try:
                staged.rename(directory)
            except OSError:
                if not await self.exists(name):
                    raise
        finally:
            if staged.exists():
                shutil.rmtree(staged)
        return name

    async def exists(self, sandbox_id: str) -> bool:
        metadata = self._directory(sandbox_id) / _METADATA
        return metadata.is_file() and not metadata.is_symlink()

    async def list(self, labels: dict[str, str]) -> list[str]:
        matches = []
        if self.root.is_dir():
            for directory in sorted(self.root.iterdir()):
                if directory.name.startswith(".") or directory.is_symlink():
                    continue
                if await self.exists(directory.name):
                    actual = self._spec(directory.name).labels
                    if all(key in actual and (not value or actual[key] == value)
                           for key, value in labels.items()):
                        matches.append(directory.name)
        return matches

    async def exec(
        self,
        sandbox_id: str,
        argv: list[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_s: float | None = None,
    ) -> AsyncIterator[ExecEvent]:
        if not argv:
            raise SandboxError("Local sandbox command is empty")
        spec = self._spec(sandbox_id)
        root = self._directory(sandbox_id)
        cwd = _path(root, workdir or spec.workdir)
        environment = {**spec.env, **(env or {})}
        for key in ("CODEX_HOME", "PI_CODING_AGENT_DIR"):
            if key in environment:
                environment[key] = _argument(root, environment[key])
        environment.update(
            HOME=str(_path(root, "/agent_home")),
            PATH=os.pathsep.join([str(root / "bin"), str(Path(sys.executable).parent),
                                  *self._host_path]),
            TMPDIR=str(_path(root, "/tmp")),
            PWD=str(cwd),
        )
        active = self._processes.setdefault(sandbox_id, set())
        try:
            spawn = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    *_command(root, argv), cwd=cwd, env=environment, start_new_session=True,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            )
            try:
                proc, cancelled = await wait_for_spawn(spawn)
            except OSError as exc:
                raise SandboxError(f"Could not run local sandbox command: {exc.strerror}") from exc
            active.add(proc)

            async def cleanup(kill: bool) -> None:
                # Also reap children left behind by a command that exited successfully.
                _kill_group(proc)
                await proc.wait()

            try:
                async with aclosing(
                    stream_process(
                        proc, cleanup, stdin=stdin, timeout_s=timeout_s, startup_cancel=cancelled
                    )
                ) as events:
                    async for event in events:
                        yield event
            finally:
                active.discard(proc)
        finally:
            if not active and self._processes.get(sandbox_id) is active:
                self._processes.pop(sandbox_id)

    async def destroy(self, sandbox_id: str) -> None:
        directory = self._directory(sandbox_id)
        active = self._processes.pop(sandbox_id, ())
        for proc in active:
            _kill_group(proc)
        await asyncio.gather(*(proc.wait() for proc in active))
        with suppress(FileNotFoundError):
            await asyncio.to_thread(shutil.rmtree, directory)

    async def upload(
        self, sandbox_id: str, target_dir: str, archive: AsyncIterator[bytes]
    ) -> None:
        self._spec(sandbox_id)
        target = _path(self._directory(sandbox_id), target_dir)
        try:
            with tempfile.TemporaryFile() as stream:
                await receive(archive, stream)
                await asyncio.to_thread(extract, stream, target)
        except (OSError, tarfile.TarError, ValueError) as exc:
            raise SandboxError("Could not extract archive into the local sandbox") from exc

    async def download(self, sandbox_id: str, path: str) -> AsyncIterator[bytes]:
        self._spec(sandbox_id)
        target = _path(self._directory(sandbox_id), path)
        try:
            with tempfile.TemporaryFile() as stream:
                if path.endswith("/."):
                    await asyncio.to_thread(pack, target, stream)
                else:
                    with tarfile.open(fileobj=stream, mode="w|") as tar:
                        await asyncio.to_thread(tar.add, target, arcname=target.name)
                stream.seek(0)
                async for chunk in chunks(stream):
                    yield chunk
        except (OSError, tarfile.TarError) as exc:
            raise SandboxError("Could not archive files from the local sandbox") from exc
