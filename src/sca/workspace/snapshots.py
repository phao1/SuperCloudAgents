"""Tenant-scoped prepared trees, published atomically and copied without hardlinks."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath

from prometheus_client import Gauge

from sca.core.errors import WorkspaceError
from sca.workspace.cache import RepoCache
from sca.workspace.layers import command, disk_bytes, permissions
from sca.workspace.tarsafe import extract

snapshot_bytes = Gauge("sca_workspace_snapshot_bytes", "Retained prepared workspace bytes")

DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def preparation_key(tenant: str, repo_key: str, commit: str, setup: str, image: str) -> str:
    return hashlib.sha256(json.dumps(
        [tenant, repo_key, commit, hashlib.sha256(setup.encode()).hexdigest(), image],
        separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def tree_digest(root: Path, key: str) -> str:
    digest = hashlib.sha256(key.encode())
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        mode = info.st_mode
        digest.update(json.dumps([
            path.relative_to(root).as_posix(), stat.S_IMODE(mode), stat.S_IFMT(mode),
            info.st_size if stat.S_ISREG(mode) else 0,
        ]).encode() + b"\0")
        if path.is_symlink():
            link = os.readlink(path)
            destination = path.parent
            # The tree moves after validation, so a link must stay inside it at each step.
            for part in PurePosixPath(link).parts:
                destination = (destination / part).resolve()
                if not destination.is_relative_to(root):
                    raise WorkspaceError("Snapshot symlink leaves the workspace")
            digest.update(link.encode() + b"\0")
        elif path.is_file():
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        elif not path.is_dir():
            raise WorkspaceError("Unsupported snapshot entry")
    return digest.hexdigest()


class SnapshotStore:
    def __init__(self, root: Path, max_bytes: int = 8 * 1024**3):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.locks = RepoCache(self.root / "locks")

    def _index(self, key: str) -> Path:
        if not DIGEST.fullmatch(key):
            raise WorkspaceError("Invalid preparation key")
        return self.root / f"{key}.json"

    def _pin(self, target: Path, identity: str) -> Path:
        refs = self.root / f"{identity}.refs"
        refs.mkdir(exist_ok=True)
        owner = str(target.resolve())
        pin = refs / hashlib.sha256(owner.encode()).hexdigest()
        pin.write_text(owner)
        return pin

    async def publish(self, key: str, archive: AsyncIterator[bytes], *,
                      base_commit: str, saved_seconds: float,
                      archive_root: str = "", retain_at: Path | None = None) -> dict:
        index = self._index(key)
        async with (self.locks._lock(self.root / "locks" / "publication"),
                    self.locks._lock(self.root / "locks" / key)):
            if index.exists():
                metadata = json.loads(index.read_text())
            else:
                metadata = await self._publish(index, key, archive, base_commit, saved_seconds,
                                               archive_root)
            if retain_at is not None:
                retain_at.mkdir(parents=True, exist_ok=True)
                self._pin(retain_at, metadata["id"])
                retain_at.with_name(f"{retain_at.name}.snapshot").write_text(metadata["id"])
            return metadata

    async def _publish(self, index: Path, key: str, archive: AsyncIterator[bytes], base_commit: str,
                       saved_seconds: float, archive_root: str) -> dict:
        with tempfile.TemporaryDirectory(prefix=".prepare-", dir=self.root) as temporary:
            directory = Path(temporary)
            size = 0
            with (directory / "archive.tar").open("w+b") as stream:
                async for chunk in archive:
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise WorkspaceError("Prepared snapshot exceeds size limit")
                    stream.write(chunk)
                stream.seek(0)
                await asyncio.to_thread(extract, stream, directory / "tree", self.max_bytes)
            tree = directory / "tree" / archive_root
            if tree.is_symlink() or not tree.is_dir():
                raise WorkspaceError("Invalid snapshot root")
            # Context belongs to the task, and is rebuilt by the kernel before setup.
            context = tree / ".sca"
            if context.is_symlink() or context.is_file():
                context.unlink()
            elif context.exists():
                await asyncio.to_thread(shutil.rmtree, context)
            identity = await asyncio.to_thread(tree_digest, tree, key)
            target = self.root / identity
            metadata = {"id": identity, "key": key, "base_commit": base_commit,
                        "saved_seconds": saved_seconds, "size_bytes": await disk_bytes(tree)}
            # Overlay checks the lower file's mode before copy-up. Preserve modes;
            # immutability comes from publication and mounting only as a lower layer.
            if not target.exists():
                tree.rename(target)
            pending = index.with_suffix(".tmp")
            pending.write_text(json.dumps(metadata))
            pending.replace(index)
            return metadata

    async def fork(self, key: str, target: Path, *, overlay: bool = False) -> dict | None:
        index = self._index(key)
        async with self.locks._lock(self.root / "locks" / key):
            if not index.exists():
                return None
            metadata = json.loads(index.read_text())
            identity = metadata["id"]
            if not DIGEST.fullmatch(identity) or metadata["key"] != key:
                raise WorkspaceError("Invalid snapshot manifest")
            tree = self.root / identity
            if not tree.is_dir() or tree.is_symlink():
                raise WorkspaceError("Missing snapshot tree")
            if overlay:
                target.mkdir(parents=True, exist_ok=True)
                source_file = target.parent / "layer-source.json"
                source = json.loads(source_file.read_text()) if source_file.exists() else {}
                source_file.write_text(json.dumps({**source, "snapshot_key": key}))
            pin = self._pin(target, identity)
            try:
                if not overlay:
                    await command("cp", "-a", "--reflink=auto", "--", str(tree), str(target))
                    await asyncio.to_thread(permissions, target, True)
                marker = target.parent / f"{target.name}.snapshot"
                marker.write_text(identity)
            except BaseException:
                pin.unlink(missing_ok=True)
                raise
            index.touch()
            return metadata

    async def prune(self, unused_hours: float) -> int:
        total = 0
        for index in self.root.glob("*.json"):
            key = index.stem
            async with self.locks._lock(self.root / "locks" / key):
                if not index.exists():
                    continue
                metadata = json.loads(index.read_text())
                identity = metadata["id"]
                if not DIGEST.fullmatch(identity):
                    raise WorkspaceError("Invalid snapshot manifest")
                tree = self.root / identity
                refs = self.root / f"{identity}.refs"
                live = False
                if refs.exists():
                    for pin in refs.iterdir():
                        target = Path(pin.read_text())
                        marker = target.parent / f"{target.name}.snapshot"
                        source_file = target.parent / "layer-source.json"
                        source = (json.loads(source_file.read_text())
                                  if source_file.is_file() else {})
                        retained_checkpoint = (target.name == "overlay"
                                               and source.get("snapshot_key") == key
                                               and (target.parent / "state.manifest.json").is_file())
                        matching_source = (target.name != "overlay"
                                           or source.get("snapshot_key") == key)
                        if (marker.is_file() and marker.read_text() == identity
                                and matching_source and (target.exists() or retained_checkpoint)):
                            live = True
                        else:
                            pin.unlink()
                if not live and time.time() - index.stat().st_mtime >= unused_hours * 3600:
                    await asyncio.to_thread(permissions, tree, True)
                    await asyncio.to_thread(shutil.rmtree, tree)
                    if refs.exists():
                        refs.rmdir()
                    index.unlink()
                else:
                    total += metadata["size_bytes"]
        # A crash between publishing the tree and its index can leave an orphan.
        async with self.locks._lock(self.root / "locks" / "publication"):
            published = {json.loads(path.read_text())["id"] for path in self.root.glob("*.json")}
            for tree in self.root.iterdir():
                if tree.is_symlink() or not tree.is_dir() or tree.name in published:
                    continue
                if not DIGEST.fullmatch(tree.name) and not tree.name.startswith(".prepare-"):
                    continue
                if time.time() - tree.stat().st_mtime >= unused_hours * 3600:
                    await asyncio.to_thread(permissions, tree, True)
                    await asyncio.to_thread(shutil.rmtree, tree)
                elif DIGEST.fullmatch(tree.name):
                    total += await disk_bytes(tree)
        snapshot_bytes.set(total)
        return total
