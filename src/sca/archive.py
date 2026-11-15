"""Move immutable task event histories to the configured object store."""

import argparse
import asyncio
import gzip
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select, update

from sca.core.errors import ConflictError
from sca.core.interfaces import ObjectStore
from sca.core.models import TERMINAL_STATES, Event
from sca.db.repo import Store
from sca.db.schema import events, tasks


def archive_key(task_id: str | UUID) -> str:
    return f"tasks/{task_id}/events.jsonl.gz"


async def read_events(objects: ObjectStore, task_id: str | UUID) -> list[Event]:
    data = gzip.decompress(await objects.get(archive_key(task_id)))
    return [Event.model_validate_json(line) for line in data.splitlines() if line]


async def archive_task(store: Store, objects: ObjectStore, task_id: str | UUID) -> None:
    async with store._transaction() as session:
        task = await store._task(session, task_id, True)
        if task["state"] not in TERMINAL_STATES:
            raise ConflictError("Only terminal tasks can be archived")
        if task["events_archived_at"] is not None:
            return
        rows = (
            await session.execute(
                select(events).where(events.c.task_id == task["id"]).order_by(events.c.seq)
            )
        ).mappings()
        history = [Event.model_validate(dict(row)) for row in rows]
        data = gzip.compress("".join(e.model_dump_json() + "\n" for e in history).encode())
        await objects.put(archive_key(task["id"]), data, "application/gzip")
        verified = await read_events(objects, task["id"])
        if verified != history:
            raise ValueError(
                "Event archive verification failed (count, sequence or content mismatch)"
            )
        await session.execute(delete(events).where(events.c.task_id == task["id"]))
        await session.execute(
            update(tasks)
            .where(tasks.c.id == task["id"])
            .values(events_archived_at=datetime.now(UTC))
        )


async def archive_finished(
    store: Store, objects: ObjectStore, older_than: timedelta, limit: int = 100
) -> int:
    if older_than < timedelta(0) or limit < 1:
        raise ValueError("Age must be nonnegative and limit positive")
    async with store._transaction() as session:
        ids = list(
            await session.scalars(
                select(tasks.c.id)
                .where(
                    tasks.c.state.in_(TERMINAL_STATES),
                    tasks.c.tenant_id == store.tenant_id if store.tenant_id is not None else True,
                    tasks.c.events_archived_at.is_(None),
                    tasks.c.updated_at < datetime.now(UTC) - older_than,
                )
                .order_by(tasks.c.updated_at)
                .limit(limit)
            )
        )
    for task_id in ids:
        await archive_task(store, objects, task_id)
    return len(ids)


async def _main(days: float, limit: int) -> None:
    from sca.db.engine import get_store

    store = get_store()
    try:
        assert store.objects is not None
        print(await archive_finished(store, store.objects, timedelta(days=days), limit))
    finally:
        await store.sessionmaker.kw["bind"].dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--older-than-days", type=float, required=True)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    asyncio.run(_main(args.older_than_days, args.limit))
