"""Trusted immutable checkouts and private reflink copies; never hardlink writable files."""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

from sca.core.errors import WorkspaceError
from sca.workspace.cache import RepoCache, _normalise


async def command(*argv: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": "/dev/null"},
    )
    try:
        async with asyncio.timeout(300):
            out, err = await proc.communicate()
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        tail = err[-400:].decode(errors="replace").strip()
        raise WorkspaceError(f"Workspace command failed: {argv[0]}" + (f": {tail}" if tail else ""))
    return out.decode().strip()


async def disk_bytes(path: Path) -> int:
    return int((await command("du", "-s", "-B1", "--", str(path))).split()[0])


def permissions(root: Path, writable: bool) -> None:
    for directory, dirs, files in os.walk(root, followlinks=False):
        for path in [Path(directory), *(Path(directory) / n for n in dirs + files)]:
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(mode | stat.S_IWUSR if writable else mode & ~0o222)


class LayerStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.locks = RepoCache(self.root / "locks")

    @staticmethod
    def key(repo_url: str, commit: str, tenant_id: str = "default") -> str:
        return hashlib.sha256(
            (tenant_id + "\0" + _normalise(repo_url) + "\0" + commit).encode()
        ).hexdigest()

    async def copy(self, mirror: Path, commit: str, identity: str, target: Path,
                   tenant_id: str = "default", *, overlay: bool = False) -> str:
        key = self.key(identity, commit, tenant_id)
        if overlay:
            key = hashlib.sha256(("overlay\0" + key).encode()).hexdigest()
        layer = self.root / key
        async with self.locks._lock(self.root / "locks" / key):
            if not layer.exists():
                temporary = Path(tempfile.mkdtemp(prefix=".prepare-", dir=self.root))
                try:
                    checkout = temporary / "tree"
                    await command("git", "-c", "core.hooksPath=/dev/null", "clone",
                                  "--no-local", "--no-checkout", "--", str(mirror), str(checkout))
                    await command("git", "-c", "core.hooksPath=/dev/null", "-C", str(checkout),
                                  "checkout", "--detach", commit)
                    if not overlay:
                        await asyncio.to_thread(permissions, checkout, False)
                    # Moving a directory requires updating its parent entry.
                    checkout.chmod(0o755)
                    checkout.rename(layer)
                    layer.chmod(0o755 if overlay else 0o555)
                finally:
                    await asyncio.to_thread(permissions, temporary, True)
                    await asyncio.to_thread(shutil.rmtree, temporary)
            refs = self.root / f"{key}.refs"
            refs.mkdir(exist_ok=True)
            owner = str(target.resolve())
            pin = refs / hashlib.sha256(owner.encode()).hexdigest()
            pin.write_text(owner)
            try:
                if overlay:
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    await command("cp", "-a", "--reflink=auto", "--", str(layer), str(target))
                    await asyncio.to_thread(permissions, target, True)
            except BaseException:
                pin.unlink(missing_ok=True)
                if target.exists():
                    await asyncio.to_thread(permissions, target, True)
                    await asyncio.to_thread(shutil.rmtree, target)
                raise
            marker = target.parent / f"{target.name}.layer"
            temporary_marker = marker.with_suffix(".layer.tmp")
            temporary_marker.write_text(key)
            temporary_marker.replace(marker)
            layer.touch()
        return key

    async def prune(self, unused_hours: float) -> int:
        total = 0
        for layer in self.root.iterdir():
            if len(layer.name) != 64 or not layer.is_dir() or layer.is_symlink():
                continue
            async with self.locks._lock(self.root / "locks" / layer.name):
                refs = self.root / f"{layer.name}.refs"
                live = 0
                if refs.exists():
                    for pin in refs.iterdir():
                        target = Path(pin.read_text())
                        marker = target.parent / f"{target.name}.layer"
                        if ((target.name == "overlay" and not target.exists())
                                or (target.exists() and marker.is_file()
                                    and marker.read_text() == layer.name)):
                            live += 1
                        else:
                            pin.unlink()
                            layer.touch()
                if not live and time.time() - layer.stat().st_mtime >= unused_hours * 3600:
                    await asyncio.to_thread(permissions, layer, True)
                    await asyncio.to_thread(shutil.rmtree, layer)
                    if refs.exists():
                        refs.rmdir()
                else:
                    total += await disk_bytes(layer)
        return total
