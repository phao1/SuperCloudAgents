import asyncio
from pathlib import Path
from uuid import UUID

from sca.core.errors import NotFoundError


def key_for(task_id: str | UUID, name: str) -> str:
    if not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("Invalid artifact name")
    return f"tasks/{UUID(str(task_id))}/{name}"


def validate_key(key: str) -> None:
    if not key or Path(key).is_absolute() or ".." in Path(key).parts or not Path(key).parts:
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
            path.write_bytes(data)

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
