"""Manifest-last snapshots; immutable blobs keep the last published state recoverable."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tempfile
from pathlib import Path

from sca.core.errors import NotFoundError, WorkspaceError
from sca.core.interfaces import ObjectStore
from sca.host import remove_tree
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

    async def _blob(self, manifest: dict, name: str) -> bytes:
        digest = manifest["sha256"][name]
        try:
            blob = await self.objects.get(f"{self.prefix}/blobs/{digest}")
        except NotFoundError as exc:
            raise WorkspaceError(f"state checksum object missing: {name}") from exc
        if hashlib.sha256(blob).hexdigest() != digest:
            raise WorkspaceError(f"state checksum mismatch: {name}")
        return blob

    async def checkpoint_bundle(self, commit: str) -> bytes:
        """Read a completed subtask's mirror without restoring its live workspace."""
        manifest = await self.manifest()
        if manifest is None or manifest["commit"] != commit:
            raise WorkspaceError("subtask checkpoint manifest does not match its result")
        return await self._blob(manifest, "mirror.bundle")

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
        from sca.workspace.overlay import OverlayWorkspace

        if isinstance(ws, OverlayWorkspace):
            data["overlay.json"] = await asyncio.to_thread(ws.snapshot)
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
            and (ws.task_dir / "workspace.ready").exists()
            and marker.exists()
            and json.loads(marker.read_bytes()) == manifest
        ):
            return False
        data = {}
        for name in ("mirror.bundle", "agent_home.tar",
                     *(["overlay.json"] if "overlay.json" in manifest["sha256"] else [])):
            data[name] = await self._blob(manifest, name)
        ws.task_dir.mkdir(parents=True, exist_ok=True)
        marker.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(dir=ws.task_dir.parent) as directory:
            staging = Path(directory)
            bundle = staging / "mirror.bundle"
            bundle.write_bytes(data["mirror.bundle"])
            rebuilt = GitWorkspace(staging, ws.branch)
            await rebuilt._git("clone", "--mirror", "-c", "core.autocrlf=false",
                               "--", str(bundle), str(rebuilt.mirror))
            await asyncio.to_thread(
                extract, io.BytesIO(data["agent_home.tar"]), staging / "agent_home"
            )
            from sca.workspace.overlay import OverlayWorkspace

            overlay = isinstance(ws, OverlayWorkspace)
            if not overlay:
                await rebuilt.build_workspace(manifest["commit"])
            names = ("mirror.git", "agent_home") if overlay else (
                "mirror.git", "workspace", "workspace.ready", "agent_home")
            for name in names:
                dest = ws.task_dir / name
                if dest.is_symlink() or dest.is_file():
                    dest.unlink()
                elif dest.exists():
                    remove_tree(dest)
                (staging / name).replace(dest)
        if not overlay:
            # Rebuilding from Git does not restore ignored setup output from a prepared fork.
            (ws.task_dir / "workspace.snapshot").unlink(missing_ok=True)
        if overlay:
            if "overlay.json" in data:
                await ws.restore_snapshot(data["overlay.json"])
            else:
                (ws.task_dir / "workspace.ready").unlink(missing_ok=True)
                await ws.build_workspace(manifest["commit"])
        marker.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        return True
