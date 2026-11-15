"""Manifest-last snapshots; immutable blobs keep the last published state recoverable."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import shutil
import tempfile
from pathlib import Path

from sca.core.errors import NotFoundError, WorkspaceError
from sca.core.interfaces import ObjectStore
from sca.workspace.git import GitWorkspace
from sca.workspace.tarsafe import extract, pack

CREDENTIAL_FILES = frozenset({"auth.json"})


class TaskStateStore:
    def __init__(self, objects: ObjectStore, task_id: str):
        self.objects = objects
        self.prefix = f"tasks/{task_id}/state"

    async def manifest(self) -> dict | None:
        try:
            return json.loads(await self.objects.get(f"{self.prefix}/manifest.json"))
        except NotFoundError:
            return None

    async def save(self, ws: GitWorkspace, turn: int, commit: str) -> None:
        with tempfile.TemporaryDirectory(dir=ws.task_dir) as directory:
            bundle = Path(directory) / "mirror.bundle"
            await ws._git("--git-dir", str(ws.mirror), "bundle", "create", str(bundle), "--all")
            home = ws.task_dir / "agent_home"
            home.mkdir(exist_ok=True)
            archive = io.BytesIO()
            # A provider credential copied in for the agent must never be persisted.
            await asyncio.to_thread(pack, home, archive, CREDENTIAL_FILES)
            data = {"mirror.bundle": bundle.read_bytes(), "agent_home.tar": archive.getvalue()}
        manifest = {"turn": turn, "commit": commit, "sha256": {}}
        for name, blob in data.items():
            digest = hashlib.sha256(blob).hexdigest()
            manifest["sha256"][name] = digest
            await self.objects.put(f"{self.prefix}/blobs/{digest}", blob)
            await self.objects.put(f"{self.prefix}/{name}", blob)
        encoded = json.dumps(manifest, sort_keys=True).encode()
        await self.objects.put(f"{self.prefix}/manifest.json", encoded, "application/json")
        (ws.task_dir / "state.manifest.json").write_bytes(encoded)

    async def restore(self, ws: GitWorkspace, *, force: bool = False) -> bool:
        manifest = await self.manifest()
        if manifest is None:
            return False
        marker = ws.task_dir / "state.manifest.json"
        if (
            not force
            and ws.mirror.exists()
            and marker.exists()
            and json.loads(marker.read_bytes()) == manifest
        ):
            return False
        data = {}
        for name in ("mirror.bundle", "agent_home.tar"):
            digest = manifest["sha256"][name]
            try:
                blob = await self.objects.get(f"{self.prefix}/blobs/{digest}")
            except NotFoundError as exc:
                raise WorkspaceError(f"state checksum object missing: {name}") from exc
            if hashlib.sha256(blob).hexdigest() != digest:
                raise WorkspaceError(f"state checksum mismatch: {name}")
            data[name] = blob
        ws.task_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ws.task_dir.parent) as directory:
            staging = Path(directory)
            bundle = staging / "mirror.bundle"
            bundle.write_bytes(data["mirror.bundle"])
            rebuilt = GitWorkspace(staging, ws.branch)
            await rebuilt._git("clone", "--mirror", "--", str(bundle), str(rebuilt.mirror))
            await asyncio.to_thread(
                extract, io.BytesIO(data["agent_home.tar"]), staging / "agent_home"
            )
            await rebuilt.build_workspace(manifest["commit"])
            for name in ("mirror.git", "workspace", "workspace.ready", "agent_home"):
                dest = ws.task_dir / name
                if dest.is_symlink() or dest.is_file():
                    dest.unlink()
                elif dest.exists():
                    shutil.rmtree(dest)
                (staging / name).replace(dest)
        marker.write_text(json.dumps(manifest, sort_keys=True))
        return True
