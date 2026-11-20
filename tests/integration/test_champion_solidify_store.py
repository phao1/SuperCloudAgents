import asyncio
import socket
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, NoResultFound

from sca.config import get_settings
from sca.db.schema import platform_settings

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def postgres_available():
    url = make_url(get_settings().database_url)
    try:
        with socket.create_connection((url.host or "localhost", url.port or 5432), timeout=2):
            pass
    except OSError as error:
        pytest.skip(f"Postgres unavailable: {type(error).__name__}")


async def test_default_is_platform_wide_and_pinned_at_first_use(store, api_store):
    configured, first, second = ("2.3.0-sca.1", "2.3.0-sca.2", "2.3.0-sca.3")

    async def task_id():
        task, _ = await store.create_task(tenant_id="customer", kind="coding",
                                          spec={}, idempotency_key=None)
        return str(task.id)

    original = await task_id()
    assert await store.resolve_task_opencode_version(original, configured) == configured

    operator = api_store.for_tenant("eval")
    await operator.set_default_opencode_version(first)
    pinned = await task_id()
    assert await asyncio.gather(*(store.resolve_task_opencode_version(pinned, configured)
                                  for _ in range(2))) == [first, first]
    await operator.set_default_opencode_version(second)
    assert await store.resolve_task_opencode_version(pinned, configured) == first
    assert await store.resolve_task_opencode_version(await task_id(), configured) == second
    assert (await store.get_task(pinned)).result["agent_version"] == first

    provisioned = await task_id()
    await store.update_result(provisioned, {"sandbox_id": "before-migration"})
    assert await store.resolve_task_opencode_version(provisioned, configured) == configured

    other = api_store.for_tenant("customer")
    async with other._transaction() as session:
        assert (await session.execute(select(platform_settings))).first() is None
    with pytest.raises(DBAPIError):
        async with other._transaction() as session:
            await session.execute(insert(platform_settings).values(
                tenant_id="eval", opencode_default_version=configured,
            ))


async def test_champion_eval_lineage_and_paired_run_rows_are_tenant_scoped(store, api_store):
    champion_id, candidate_id, evolution_id, original_id = (uuid4() for _ in range(4))
    worker = store.for_tenant("eval")
    await worker.create_evolution({
        "id": evolution_id, "status": "completed", "config": {}, "created_by": "operator",
    })
    original_variant = {"suite": "/eval/suite", "split": "dev", "repeat": 2}
    await worker.create_eval_run({
        "id": original_id, "tenant_id": "eval", "suite": "suite", "suite_hash": "original-suite",
        "variant": original_variant, "created_by": "operator", "status": "completed",
    })
    await worker.save_evolution_candidate({
        "id": candidate_id, "evolution_id": evolution_id, "generation": 1, "decision": "promoted",
        "overlay": {}, "overlay_sha256": "hash", "hypothesis": "test", "diagnosis": {},
        "static_check": {}, "dev_run_id": original_id,
    })
    await worker.activate_champion(str(champion_id), {
        "agent_version": "2.3.0-sca.1", "model": "fixture-model", "overlay": {},
        "overlay_sha256": "hash", "metrics": {}, "candidate_id": candidate_id,
    })

    eval_api = api_store.for_tenant("eval")
    assert (await eval_api.get_champion(champion_id))["id"] == champion_id
    assert await eval_api.get_champion_eval(champion_id) == {
        "suite_hash": "original-suite", "variant": original_variant,
    }
    other = api_store.for_tenant("customer")
    with pytest.raises(NoResultFound):
        await other.get_champion(champion_id)
    with pytest.raises(NoResultFound):
        await other.get_champion_eval(champion_id)

    runs = [{"id": uuid4(), "suite": "suite", "suite_hash": "original-suite",
             "variant": {"split": None}, "created_by": "operator", "status": "queued"}
            for _ in range(2)]
    await worker.create_solidify_eval_runs(runs)
    await worker.create_solidify_eval_runs(runs)
    for run in runs:
        found = await eval_api.get_eval_run(str(run["id"]))
        assert found["status"] == "queued"
        assert found["tenant_id"] == "eval"
        with pytest.raises(NoResultFound):
            await other.get_eval_run(str(run["id"]))
