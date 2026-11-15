import asyncio
import logging
from collections import defaultdict
from functools import lru_cache

import asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from sca.config import get_settings
from sca.db.repo import Store
from sca.storage import make_object_store


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


@lru_cache
def get_store() -> Store:
    settings = get_settings()
    return Store(
        make_sessionmaker(make_engine(settings.database_url)),
        getattr(settings, "default_max_active_tasks", 20),
        objects=make_object_store(settings),
    )


class EventListener:
    """One LISTEN connection, with independent wakeups for each stream."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.waiters: dict[str, set[asyncio.Event]] = defaultdict(set)
        self.connected = False

    def subscribe(self, task_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self.waiters[task_id].add(event)
        return event

    def unsubscribe(self, task_id: str, event: asyncio.Event) -> None:
        self.waiters[task_id].discard(event)
        if not self.waiters[task_id]:
            del self.waiters[task_id]

    def _notify(self, connection: asyncpg.Connection, pid: int, channel: str, payload: str) -> None:
        for event in self.waiters.get(payload, ()):
            event.set()

    async def run(self) -> None:
        url = make_url(self.url)
        delay = 0.1
        while True:
            connection = None
            try:
                connection = await asyncpg.connect(
                    host=url.host,
                    port=url.port or 5432,
                    user=url.username,
                    password=url.password,
                    database=url.database,
                    timeout=5,
                )
                disconnected = asyncio.Event()
                connection.add_termination_listener(lambda _, signal=disconnected: signal.set())
                await connection.add_listener("sca_events", self._notify)
                self.connected = True
                delay = 0.1
                await disconnected.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning("Event listener disconnected", exc_info=True)
            finally:
                self.connected = False
                if connection is not None:
                    await connection.close(timeout=2)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5)
