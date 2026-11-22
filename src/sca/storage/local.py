import asyncio
import hashlib
import os
import threading
from pathlib import Path
from uuid import UUID

from sca.core.errors import NotFoundError


def key_for(task_id: str | UUID, name: str) -> str:
    path = Path(name)
    if not name or path.anchor or ".." in path.parts:
        raise ValueError("Invalid artifact name")
    return f"tasks/{UUID(str(task_id))}/{name}"


def validate_key(key: str) -> None:
    path = Path(key)
    if not path.parts or path.anchor or ".." in path.parts:
        raise ValueError("Object key must be a relative path inside the store")


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = (self.root / key).resolve()
        if Path(key).is_absolute() or not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("Object key must be a relative path inside the store")
        return path

    async def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> str:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Readers must never see a half-written object (the task state manifest relies on it).
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)

        await asyncio.to_thread(write)
        return path.as_uri()

    async def get(self, key: str) -> bytes:
        try:
            return await asyncio.to_thread(self._path(key).read_bytes)
        except FileNotFoundError:
            raise NotFoundError("Object not found") from None

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)

    async def list(self, prefix: str = "") -> list[str]:
        if prefix:
            self._path(prefix)

        def listing() -> list[str]:
            return sorted(
                path.relative_to(self.root).as_posix()
                for path in self.root.rglob("*")
                if path.is_file()
                and path.relative_to(self.root).as_posix().startswith(prefix)
                and path.resolve().is_relative_to(self.root)
            )

        return await asyncio.to_thread(listing)


# History transfer
def history_artifact_key(task_id: str | UUID, name: str) -> str:
    # An imported name can contain characters or case distinctions unavailable on Windows.
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return key_for(task_id, f"history-artifacts/{digest}")
