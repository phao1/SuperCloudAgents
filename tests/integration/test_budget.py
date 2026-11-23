import pytest

pytestmark = pytest.mark.integration


async def test_budgets(store):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from sca.db.schema import tasks

    async def create(tenant, spec):
        return (
            await store.create_task(
                tenant_id=tenant, kind="coding", spec=spec, idempotency_key=None
            )
        )[0]

    task = await create("one", {"max_tokens": 10})
    assert await store.check_budget(task.id) is None
    await store.add_usage(task.id, {"input_tokens": 4, "output_tokens": 6})
    assert "Task" in await store.check_budget(task.id)
    other = await create("one", {})
    await store.set_tenant_budget("one", 15)
    assert await store.check_budget(other.id) is None
    await store.add_usage(other.id, {"output_tokens": 5})
    assert "Tenant" in await store.check_budget(other.id)
    separate = await create("two", {})
    assert await store.check_budget(separate.id) is None
    async with store._transaction() as session:
        await session.execute(
            update(tasks)
            .where(tasks.c.id == task.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=1))
        )
    assert await store.check_budget(other.id) is None
    await store.set_tenant_budget("one", None)
    await store.add_usage(other.id, {"input_tokens": 100})
    assert await store.check_budget(other.id) is None


async def test_cost_budgets(store):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from sca.db.schema import tasks

    async def create(tenant, spec):
        return (
            await store.create_task(
                tenant_id=tenant, kind="coding", spec=spec, idempotency_key=None
            )
        )[0]

    task = await create("cost", {"max_cost_usd": 0.01})
    await store.add_usage(task.id, {"cost_micro_usd": 9999})
    assert await store.check_budget(task.id) is None
    await store.add_usage(task.id, {"cost_micro_usd": 1})
    assert "Task cost budget" in await store.check_budget(task.id)
    other = await create("cost", {})
    await store.set_tenant_budget("cost", daily_cost_budget_usd=0.02)
    assert await store.check_budget(other.id) is None
    await store.add_usage(other.id, {"cost_micro_usd": 10000})
    assert "Tenant daily cost budget" in await store.check_budget(other.id)
    separate = await create("separate", {})
    assert await store.check_budget(separate.id) is None
    async with store._transaction() as session:
        await session.execute(
            update(tasks)
            .where(tasks.c.id == task.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=1))
        )
    assert await store.check_budget(other.id) is None
    await store.set_tenant_budget("cost", daily_cost_budget_usd=None)
    assert await store.check_budget(other.id) is None


async def test_turn_reservations_hold_budget_until_spend_is_recorded(store):
    import asyncio

    await store.set_tenant_budget("settlement", daily_cost_budget_usd=.20)
    task, _ = await store.create_task(
        tenant_id="settlement", kind="coding", spec={"max_cost_usd": .16}, idempotency_key=None,
    )
    other, _ = await store.create_task(
        tenant_id="settlement", kind="coding", spec={}, idempotency_key=None,
    )
    task_id, other_id = str(task.id), str(other.id)
    event = {"turn": 1, "model": "fixture"}
    assert await store.reserve_turn_spend(task_id, "old", .12, event) == (.12, False)
    assert await store.reserve_turn_spend(task_id, "new", .12, {"turn": 2}) == (.04, False)
    assert await store.reserve_turn_spend(other_id, "other", .12, event) == (.04, False)
    assert await store.reserve_turn_spend(task_id, "blocked", .12, {"turn": 3}) == (0, True)
    assert (await store.get_task(task_id)).usage == {}
    charges = await asyncio.gather(*[store.apply_turn_spend(task_id, "old", .03) for _ in range(2)])
    assert sorted(charges) == [False, True]
    assert await store.reserve_turn_spend(task_id, "next", .12, {"turn": 3}) == (.09, False)
    assert (await store.get_task(task_id)).usage["cost_micro_usd"] == 30_000
    assert [event.payload["cost_micro_usd"] for event in await store.list_events(task_id)
            if event.type == "agent.usage"] == [30_000]
