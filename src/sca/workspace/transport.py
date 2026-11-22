from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from sca.core.errors import WorkspaceError
from sca.core.interfaces import SandboxProvider
from sca.core.models import ExecExit, Mount
from sca.host import remove_tree
from sca.workspace.git import GitWorkspace
from sca.workspace.tarsafe import chunks, extract, pack, receive


class MountTransport:
    def __init__(self, sandbox: SandboxProvider):
        self.sandbox = sandbox

    def mounts(self, task_dir: Path) -> list[Mount]:
        if (task_dir / "workspace.overlay").exists():
            from sca.workspace.overlay import volume_name

            context = task_dir / "workspace" / ".sca"
            context.mkdir(parents=True, exist_ok=True)
            return [Mount(source=volume_name(task_dir), target="/workspace"),
                    Mount(source=str(context), target="/workspace/.sca")]
        return [Mount(source=str(task_dir / "workspace"), target="/workspace")]

    async def push(self, sandbox_id: str, task_dir: Path) -> None:
        pass

    async def pull_commit(self, sandbox_id: str, ws: GitWorkspace) -> str:
        return await ws.fetch()

    async def pull_agent_home(self, sandbox_id: str, task_dir: Path) -> None:
        pass


class SyncTransport(MountTransport):
    def mounts(self, task_dir: Path) -> list[Mount]:
        return []

    async def push(self, sandbox_id: str, task_dir: Path) -> None:
        for name in ("workspace", "agent_home"):
            source = task_dir / name
            source.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile() as archive:
                await asyncio.to_thread(pack, source, archive)
                archive.seek(0)
                await self.sandbox.upload(sandbox_id, f"/{name}", chunks(archive))

    async def pull_commit(self, sandbox_id: str, ws: GitWorkspace) -> str:
        result = None
        async for event in self.sandbox.exec(
            sandbox_id,
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "bundle",
                "create",
                "/tmp/sca.bundle",
                "HEAD",
            ],
            workdir="/workspace",
        ):
            if isinstance(event, ExecExit):
                result = event
        if result is None or result.exit_code:
            raise WorkspaceError("could not create sandbox bundle")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with tempfile.TemporaryFile() as archive:
                await receive(self.sandbox.download(sandbox_id, "/tmp/sca.bundle"), archive)
                await asyncio.to_thread(extract, archive, target)
            return await ws.fetch_bundle(target / "sca.bundle")

    async def pull_agent_home(self, sandbox_id: str, task_dir: Path) -> None:
        with tempfile.TemporaryDirectory(dir=task_dir) as directory:
            target = Path(directory)
            with tempfile.TemporaryFile() as archive:
                await receive(self.sandbox.download(sandbox_id, "/agent_home"), archive)
                await asyncio.to_thread(extract, archive, target)
            home = task_dir / "agent_home"
            if home.exists():
                remove_tree(home)
            (target / "agent_home").replace(home)
