"""Share worker activity timestamps with the API on the host data directory."""

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

LiveEntity = Literal["task", "evolution", "evaluation"]
LaneScope = tuple[str, int | None]
MAIN: LaneScope = ("", None)
HEARTBEAT_SECONDS = 10


@dataclass(frozen=True)
class WorkerBeat:
    operation: str
    scope: LaneScope
    started_at: datetime
    at: datetime


def _directory(data_dir: Path, entity: LiveEntity, id_: str | UUID) -> Path:
    return data_dir / "run" / "live" / f"{entity}-{UUID(str(id_))}"


def _register(directory: Path, operation: str, scope: LaneScope) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".json", mode="w", encoding="utf-8",
                                     delete=False) as marker:
        path = Path(marker.name)
        try:
            json.dump({"operation": operation, "scope": scope, "started_at": datetime.now(UTC).isoformat()}, marker)
        except BaseException:
            marker.close()
            path.unlink(missing_ok=True)
            raise
    return path


@asynccontextmanager
async def worker_activity(
    data_dir: Path, entity: LiveEntity, id_: str | UUID, operation: str, scope: LaneScope = MAIN,
) -> AsyncIterator[None]:
    try:
        path = _register(_directory(data_dir, entity, id_), operation, scope)
    except (OSError, ValueError):
        yield
        return

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            with suppress(OSError):
                path.touch(exist_ok=True)

    beater = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        beater.cancel()
        with suppress(asyncio.CancelledError):
            await beater
        # A process that stops unexpectedly leaves its last timestamp for the API to read.
        with suppress(OSError):
            path.unlink(missing_ok=True)
            path.parent.rmdir()


def read_beats(data_dir: Path, entity: LiveEntity, id_: UUID) -> list[WorkerBeat]:
    beats = []
    try:
        files = list(_directory(data_dir, entity, id_).glob("*.json"))
    except OSError:
        return beats
    for path in files:
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            name, candidate = info["scope"]
            if not isinstance(name, str) or (candidate is not None and type(candidate) is not int):
                continue
            started = datetime.fromisoformat(info["started_at"])
            if started.tzinfo is not None and isinstance(operation := info["operation"], str):
                beats.append(WorkerBeat(operation, (name, candidate), started,
                                        datetime.fromtimestamp(path.stat().st_mtime, UTC)))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return beats
