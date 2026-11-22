from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sca.core.errors import InvalidTaskError, WorkspaceError
from sca.core.models import Mount, RepoCredential
from sca.host import lock_file, remove_tree, unlock_file
from sca.secrets import git_env, redact

PIP_CACHE_ENV = {"PIP_CACHE_DIR": "/cache/pip"}


def pip_cache_mount(root: Path, tenant_id: str) -> Mount:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", tenant_id):
        raise ValueError("tenant_id must be a single safe path component")
    path = root.resolve() / "pip" / tenant_id
    path.mkdir(parents=True, exist_ok=True)
    return Mount(source=str(path), target="/cache/pip")


def _normalise(url: str) -> str:
    if "://" not in url:
        path = Path(url)
        return str(path.resolve()) if path.is_absolute() or ":" not in url else url
    parts = urlsplit(url)
    # Preserve credentials and case-sensitive repository paths.
    userinfo, separator, host = parts.netloc.rpartition("@")
    authority = userinfo + separator + host.lower()
    return urlunsplit((parts.scheme.lower(), authority, parts.path.rstrip("/"), parts.query, ""))


class RepoCache:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, repo_url: str, tenant_id: str = "default") -> Path:
        key = hashlib.sha256((tenant_id + "\0" + _normalise(repo_url)).encode()).hexdigest()
        return self.root / f"{key}.git"

    @asynccontextmanager
    async def _lock(self, path: Path) -> AsyncIterator[None]:
        # Lock files are permanent: unlinking one could split waiters across inodes.
        with path.with_suffix(".lock").open("a+b") as lock:
            while True:
                try:
                    lock_file(lock, blocking=False)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            try:
                yield
            finally:
                unlock_file(lock)

    async def _git(self, *args: str, credential: RepoCredential | None = None) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                *(["-c", "credential.helper="] if credential else []),
                *args,
                env={**git_env(credential), "LC_ALL": "C"},
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await proc.communicate()
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                raise
        except OSError as exc:
            raise WorkspaceError(redact(str(exc), credential)) from exc
        if proc.returncode:
            raise InvalidTaskError(redact(err.decode("utf-8", errors="replace").strip(), credential))

    async def _ensure(
        self, repo_url: str, path: Path, credential: RepoCredential | None = None
    ) -> None:
        if path.exists():
            try:
                await self._git(
                    "--git-dir", str(path), "fetch", "--prune", "origin", credential=credential
                )
            except InvalidTaskError as exc:
                # Network/auth failures must not destroy a usable mirror.
                network = (
                    "could not resolve",
                    "unable to access",
                    "connection",
                    "timed out",
                    "authentication",
                    "permission denied",
                    "repository not found",
                    "could not read from remote",
                    "does not appear to be a git repository",
                )
                if (
                    any(marker in str(exc).lower() for marker in network)
                    and (path / "HEAD").is_file()
                    and (path / "objects").is_dir()
                ):
                    raise
                await asyncio.to_thread(remove_tree, path)
            else:
                path.touch()
                return
        try:
            await self._git("clone", "--mirror", "-c", "core.autocrlf=false", "--",
                            repo_url, str(path), credential=credential)
        except BaseException:
            if path.exists():
                await asyncio.to_thread(remove_tree, path)
            raise
        path.touch()

    @asynccontextmanager
    async def mirror(
        self, repo_url: str, *, tenant_id: str = "default", credential: RepoCredential | None = None
    ) -> AsyncIterator[Path]:
        path = self._path(repo_url, tenant_id)
        async with self._lock(path):
            await self._ensure(repo_url, path, credential)
            yield path

    async def ensure(
        self, repo_url: str, *, tenant_id: str = "default", credential: RepoCredential | None = None
    ) -> Path:
        async with self.mirror(repo_url, tenant_id=tenant_id, credential=credential) as path:
            return path

    async def prune(self, max_entries: int) -> None:
        if max_entries < 0:
            raise ValueError("max_entries must be non-negative")
        entries = sorted(self.root.glob("*.git"), key=lambda p: p.stat().st_mtime_ns)
        for path in entries[: max(0, len(entries) - max_entries)]:
            async with self._lock(path):
                # An ensure may have refreshed the entry while prune waited.
                current = sorted(self.root.glob("*.git"), key=lambda p: p.stat().st_mtime_ns)
                if path in current[: max(0, len(current) - max_entries)]:
                    await asyncio.to_thread(remove_tree, path)
