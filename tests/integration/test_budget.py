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
