"""Housekeeping against Postgres and a real Temporal server, with external-resource fakes."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from sca.archive import read_events
from sca.config import Settings
from sca.core.models import TaskState
from sca.maintenance import MaintenanceActivities, MaintenanceWorkflow
from sca.observability import Metrics
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


class Sandbox:
    def __init__(self, ids):
        self.ids = ids
        self.destroyed = []

    async def list(self, labels):
        return [
            name
            for name, task_id in self.ids.items()
            if not labels["sca.task_id"] or labels["sca.task_id"] == task_id
        ]

    async def destroy(self, sid):
        self.destroyed.append(sid)


class Gateway:
    def __init__(self, aliases):
        self.aliases = aliases
        self.deleted = []

    async def list_aliases(self, prefix):
        return [a for a in self.aliases if a.startswith(prefix)]

    async def revoke_key(self, alias):
        self.deleted.append(alias)


@pytest.mark.parametrize("fail_prune", [False, True])
async def test_maintenance(store, tmp_path, fail_prune):
    try:
        async with asyncio.timeout(5):
            client = await Client.connect("localhost:7233")
    except (TimeoutError, RuntimeError, OSError):
        pytest.skip("Temporal unavailable at localhost:7233")
    finished, _ = await store.create_task(
        tenant_id="default", kind="coding", spec={}, idempotency_key=None
    )
    running, _ = await store.create_task(
        tenant_id="default", kind="coding", spec={}, idempotency_key=None
    )
    await store.set_state(finished.id, TaskState.FAILED)
    await store.set_state(running.id, TaskState.PROVISIONING)
    await store.set_state(running.id, TaskState.RUNNING)
    async with store._transaction() as session:
        await session.execute(
            text("UPDATE tasks SET updated_at = :old WHERE id = :id"),
            {"old": datetime.now(UTC) - timedelta(days=31), "id": finished.id},
        )
    missing = str(uuid4())
    ids = {f"sca-task-{i}": str(i) for i in (finished.id, running.id, missing)}
    ids["unrecognized"] = missing
    ids[f"sca-task-{finished.id}-child"] = str(finished.id)
    ids[f"sca-task-{running.id}-child"] = str(running.id)
    sandbox = Sandbox(ids)
    gateway = Gateway([f"task-{i}-turn1-attempt1" for i in (finished.id, running.id)])
    objects = LocalObjectStore(tmp_path / "objects")
    settings = Settings(data_dir=tmp_path, maintenance_schedule=False)
    activities = MaintenanceActivities(settings, store, objects, sandbox, gateway)
    registered = activities.all()
    cache = tmp_path / "cache" / "repos" / "old.git"
    cache.mkdir(parents=True)
    settings.repo_cache_max_entries = 0
    if fail_prune:

        @activity.defn(name="prune_repo_cache")
        async def fail():
            raise RuntimeError("prune deliberately failed")

        registered[1] = fail
    queue = f"maintenance-{uuid4()}"
    async with Worker(
        client, task_queue=queue, workflows=[MaintenanceWorkflow], activities=registered
    ):
        await client.execute_workflow(MaintenanceWorkflow.run, id=str(uuid4()), task_queue=queue)
    assert await read_events(objects, finished.id)
    assert set(sandbox.destroyed) == {
        f"sca-task-{finished.id}",
        f"sca-task-{missing}",
        f"sca-task-{finished.id}-child",
    }
    assert gateway.deleted == [f"task-{finished.id}-turn1-attempt1"]
    assert cache.exists() == fail_prune


async def test_oldest_age_rls_and_cache(store, api_store):
    task, _ = await store.create_task(
        tenant_id="other-tenant", kind="coding", spec={}, idempotency_key=None
    )
    await store.set_state(task.id, TaskState.PROVISIONING)
    async with store._transaction() as session:
        await session.execute(
            text("UPDATE tasks SET created_at = :old WHERE id = :id"),
            {"old": datetime.now(UTC) - timedelta(hours=3), "id": task.id},
        )
    metrics = Metrics()
    await metrics.render(api_store)
    assert metrics.oldest_active._value.get() >= 10800
    await store.set_state(task.id, TaskState.FAILED)
    await metrics.render(api_store)
    assert metrics.oldest_active._value.get() >= 10800
    metrics._expires = 0
    await metrics.render(api_store)
    assert metrics.oldest_active._value.get() == 0


async def test_schedule_creation_is_idempotent():
    from temporalio.client import ScheduleOverlapPolicy

    from sca.orchestration.worker import ensure_maintenance_schedule

    try:
        async with asyncio.timeout(5):
            client = await Client.connect("localhost:7233")
    except (TimeoutError, RuntimeError, OSError):
        pytest.skip("Temporal unavailable at localhost:7233")
    schedule_id = f"test-maintenance-{uuid4()}"

    class IsolatedClient:
        async def create_schedule(self, name, schedule):
            assert name == "sca-maintenance"
            return await client.create_schedule(schedule_id, schedule)

    settings = Settings(maintenance_schedule=False)
    try:
        await ensure_maintenance_schedule(IsolatedClient(), settings)
        await ensure_maintenance_schedule(IsolatedClient(), settings)
        description = await client.get_schedule_handle(schedule_id).describe()
        assert description.schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
        assert description.schedule.spec.intervals[0].every == timedelta(hours=1)
    finally:
        await client.get_schedule_handle(schedule_id).delete()


async def test_replenish_pool_activity_settings():
    from unittest.mock import AsyncMock

    from sca.sandbox.k8s import K8sSandboxProvider

    sandbox = K8sSandboxProvider()
    sandbox.replenish = AsyncMock()
    settings = Settings(_env_file=None, k8s_pool_size=2, state_transport="sync")
    activities = MaintenanceActivities(settings, None, None, sandbox, None)
    await activities.replenish_sandbox_pool()
    spec, size = sandbox.replenish.call_args.args
    assert size == sandbox.pool_size == 2
    assert spec.image == settings.sandbox_image
    assert spec.network == settings.sandbox_network
    assert spec.labels == {"sca.state_transport": "sync"}
    assert spec.env == {} and spec.mounts == []
    settings.state_transport = "mount"
    await activities.replenish_sandbox_pool()
    assert sandbox.replenish.call_args.args[1] == 0
