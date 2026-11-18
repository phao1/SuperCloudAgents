import asyncio

import pytest
from sqlalchemy import text

from sca.config import get_settings
from sca.core.models import TaskState
from sca.db.repo import QuotaExceededError

pytestmark = pytest.mark.integration


async def create(store, tenant="a", priority="interactive"):
    task, _ = await store.for_tenant(tenant).create_task(
        tenant_id=tenant, kind="coding", spec={"priority": priority}, idempotency_key=None)
    return str(task.id)


async def test_quota_wait_release_and_reacquire(store):
    await store.set_tenant_quota("a", 1)
    a, b = await create(store), await create(store)
    assert (await store.acquire_slot(a, 4))["granted"]
    assert not (await store.acquire_slot(b, 4))["granted"]
    await store.set_state(a, TaskState.PROVISIONING)
    await store.set_state(a, TaskState.RUNNING)
    await store.set_state(a, TaskState.VERIFYING)
    await store.set_state(a, TaskState.AWAITING_REVIEW)
    assert (await store.acquire_slot(b, 4))["granted"]
    assert not (await store.acquire_slot(a, 4))["granted"]
    await store.release_slot(b)
    assert (await store.acquire_slot(a, 4))["granted"]


async def test_fairness_and_priority(store):
    flood = [await create(store) for _ in range(8)]
    lone = await create(store, "b")
    assert (await store.acquire_slot(flood[0], 4))["granted"]
    assert not (await store.acquire_slot(flood[1], 4))["granted"]
    assert (await store.acquire_slot(lone, 4))["granted"]
    batch = await create(store, "c", "batch")
    evaluation = await create(store, "c", "eval")
    interactive = await create(store, "c")
    assert not (await store.acquire_slot(batch, 4))["granted"]
    assert not (await store.acquire_slot(evaluation, 4))["granted"]
    # The new tenant has no held slots and wins over the flood.
    assert (await store.acquire_slot(interactive, 4))["granted"]
    for task in flood:
        await store.set_state(task, TaskState.CANCELLED)
    await store.release_slot(interactive)
    assert (await store.acquire_slot(evaluation, 4))["granted"]
    assert (await store.acquire_slot(batch, 4))["granted"]


async def test_cancel_waiting_pressure_and_concurrency(store):
    ids = [await create(store, str(i)) for i in range(8)]
    assert not (await store.acquire_slot(ids[0], 2, saturated=True))["granted"]
    await store.set_state(ids[0], TaskState.CANCELLED)
    assert (await store.acquire_slot(ids[0], 2)).get("terminal")
    results = await asyncio.gather(*(store.acquire_slot(i, 2) for i in ids[1:]))
    assert sum(r["granted"] for r in results) <= 2
    async with store._transaction() as session:
        assert await session.scalar(text("SELECT count(*) FROM tasks WHERE slot_held")) <= 2
    events = await store.list_events(ids[0])
    assert sum(e.type == "admission.waiting" for e in events) == 1


async def test_hard_backlog_and_tenant_isolation(store, api_store, monkeypatch):
    monkeypatch.setattr(get_settings(), "tenant_max_queued", 2)
    await create(api_store)
    await create(api_store)
    with pytest.raises(QuotaExceededError):
        await create(api_store)
    await create(api_store, "b")
    with pytest.raises(ValueError, match="worker"):
        await api_store.for_tenant("a").acquire_slot("unused", 2)


async def test_weighted_tenant_order(store):
    for _ in range(2):
        assert (await store.acquire_slot(await create(store, "a"), 4))["granted"]
    assert (await store.acquire_slot(await create(store, "b"), 4))["granted"]
    async with store._transaction() as session:
        await session.execute(text("UPDATE tenants SET weight = 4 WHERE id = 'a'"))
    b = await create(store, "b")
    a = await create(store, "a")
    assert not (await store.acquire_slot(b, 4))["granted"]
    assert (await store.acquire_slot(a, 4))["granted"]
