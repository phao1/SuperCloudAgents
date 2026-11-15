import pytest

pytestmark = pytest.mark.integration


async def test_listener_fanout(store):
    import asyncio
    import time

    from sca.db.engine import EventListener

    listener = EventListener(
        store.sessionmaker.kw["bind"].url.render_as_string(hide_password=False)
    )
    task, _ = await store.create_task(
        tenant_id="default", kind="coding", spec={}, idempotency_key=None
    )
    first = listener.subscribe(str(task.id))
    second = listener.subscribe(str(task.id))
    unrelated = listener.subscribe("other")
    runner = asyncio.create_task(listener.run())
    try:
        async with asyncio.timeout(3):
            while not listener.connected:
                await asyncio.sleep(0.01)
        start = time.monotonic()
        await store.append_event(task.id, "test", {})
        await asyncio.wait_for(asyncio.gather(first.wait(), second.wait()), 1)
        assert time.monotonic() - start < 1
        assert not unrelated.is_set()
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        listener.unsubscribe(str(task.id), first)
        listener.unsubscribe(str(task.id), second)
        listener.unsubscribe("other", unrelated)
    assert not listener.connected


async def test_listener_reconnects_and_notifications_commit(store, monkeypatch):
    import asyncio

    import asyncpg

    from sca.core.models import TaskState
    from sca.db.engine import EventListener

    connections = []
    connect = asyncpg.connect

    async def tracked_connect(*args, **kwargs):
        connection = await connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(asyncpg, "connect", tracked_connect)
    listener = EventListener(
        store.sessionmaker.kw["bind"].url.render_as_string(hide_password=False)
    )
    task, _ = await store.create_task(
        tenant_id="default", kind="coding", spec={}, idempotency_key=None
    )
    wakeup = listener.subscribe(str(task.id))
    runner = asyncio.create_task(listener.run())
    try:
        async with asyncio.timeout(3):
            while not listener.connected:
                await asyncio.sleep(0.01)
        with pytest.raises(RuntimeError):
            async with store._transaction() as session:
                await store._task(session, task.id, True)
                await store._event(session, task.id, "rollback", {})
                raise RuntimeError("rollback")
        await asyncio.sleep(0.05)
        assert not wakeup.is_set()
        connections[0].terminate()
        async with asyncio.timeout(3):
            while len(connections) < 2 or not listener.connected:
                await asyncio.sleep(0.01)
        await store.set_state(task.id, TaskState.CANCELLED)
        await asyncio.wait_for(wakeup.wait(), 1)
        assert all(event.type != "rollback" for event in await store.list_events(task.id))
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
