import pytest

pytestmark = pytest.mark.integration


async def test_archive(store, tmp_path):
    from datetime import timedelta

    import httpx
    from sqlalchemy import func, select

    from sca.api.app import create_app
    from sca.archive import archive_finished, archive_task
    from sca.core.errors import ConflictError
    from sca.core.models import TaskState
    from sca.db.schema import events
    from sca.storage.local import LocalObjectStore

    objects = LocalObjectStore(tmp_path)
    store.objects = objects
    task, _ = await store.create_task(
        tenant_id="default", kind="coding", spec={}, idempotency_key=None
    )
    with pytest.raises(ConflictError):
        await archive_task(store, objects, task.id)
    await store.append_event(task.id, "test", {"text": "你好"})
    await store.set_state(task.id, TaskState.CANCELLED)
    app = create_app(store=store, orchestrator=object(), object_store=objects)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer dev-token"},
    ) as client:
        url = f"/v1/tasks/{task.id}/events"
        before = (await client.get(url)).json()
        assert await archive_finished(store, objects, timedelta(0), 1) == 1
        await archive_task(store, objects, task.id)
        assert (await client.get(url)).json() == before
        assert (await client.get(url + "?after=1&limit=1")).json() == before[1:2]
        assert "id: 3" in (await client.get(url + "/stream")).text
    async with store._transaction() as session:
        assert await session.scalar(select(func.count()).select_from(events)) == 0
    with pytest.raises(ConflictError):
        await store.append_event(task.id, "late", {})


async def test_failed_verification(store, tmp_path):
    import gzip

    from sca.archive import archive_task
    from sca.core.models import TaskState
    from sca.storage.local import LocalObjectStore

    class CorruptStore(LocalObjectStore):
        async def get(self, key):
            return gzip.compress(b"")

    task, _ = await store.create_task(
        tenant_id="default", kind="coding", spec={}, idempotency_key=None
    )
    await store.set_state(task.id, TaskState.CANCELLED)
    before = await store.list_events(task.id)
    with pytest.raises(ValueError, match="verification"):
        await archive_task(store, CorruptStore(tmp_path), task.id)
    assert await store.list_events(task.id) == before
