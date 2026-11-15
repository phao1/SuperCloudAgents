import asyncio
from pathlib import Path

import pytest

from sca.core.errors import ConflictError, NotFoundError
from sca.core.models import TaskState
from sca.storage.local import LocalObjectStore, key_for

pytestmark = pytest.mark.integration


async def new_task(store, key=None):
    return await store.create_task(tenant_id="default", kind="coding", spec={}, idempotency_key=key)


async def test_concurrent_events(store):
    task, _ = await new_task(store)
    await asyncio.gather(*(store.append_event(task.id, "test", {"n": n}) for n in range(30)))
    events = await store.list_events(str(task.id))
    assert [e.seq for e in events] == list(range(1, 32))
    results = await asyncio.gather(
        *(store.append_event(task.id, "once", {}, "key") for _ in range(10))
    )
    assert sum(e is not None for e in results) == 1
    assert len(await store.list_events(task.id)) == 32


async def test_create_and_states(store):
    results = await asyncio.gather(*(new_task(store, "same") for _ in range(8)))
    assert len({t.id for t, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    task = results[0][0]
    assert len(await store.list_events(task.id)) == 1
    with pytest.raises(NotFoundError):
        await store.get_task(task.id, "other")
    task = await store.set_state(task.id, TaskState.PROVISIONING, "ready")
    same = await store.set_state(task.id, TaskState.PROVISIONING, "ignored")
    assert same.updated_at == task.updated_at and same.state_reason == "ready"
    with pytest.raises(ConflictError):
        await store.set_state(task.id, TaskState.SUCCEEDED)
    assert len(await store.list_events(task.id)) == 2
    assert len(await store.list_tasks("default", TaskState.PROVISIONING)) == 1
    assert not await store.list_tasks("other")


async def test_results_sessions_artifacts(store, tmp_path):
    task, _ = await new_task(store)
    await store.update_result(task.id, {"a": 1})
    await store.update_result(task.id, {"b": 2})
    await asyncio.gather(*(store.add_usage(task.id, {"input_tokens": 2}) for _ in range(5)))
    result = await store.get_task(task.id)
    assert result.result == {"a": 1, "b": 2}
    assert result.usage == {"input_tokens": 10}
    assert await store.get_session_id(task.id) is None
    await asyncio.gather(*(store.set_session(task.id, "fake", str(i)) for i in range(5)))
    await store.set_session(task.id, "fake", "last")
    assert await store.get_session_id(task.id) == "last"
    objects = LocalObjectStore(tmp_path)
    key = key_for(task.id, "changes.patch")
    uri = await objects.put(key, b"patch")
    assert uri == (tmp_path / key).as_uri()
    assert await objects.get(key) == b"patch"
    for bad in ("../escape", "/tmp/escape"):
        with pytest.raises(ValueError):
            await objects.put(bad, b"bad")
    (tmp_path / "link").symlink_to(Path("/tmp"))
    with pytest.raises(ValueError):
        await objects.get("link/escape")
    with pytest.raises(NotFoundError):
        await objects.get("missing")
    await store.add_artifact(task.id, "changes.patch", "text/plain", uri, 5)
    await store.add_artifact(task.id, "changes.patch", "text/plain", uri, 6)
    assert len(await store.list_artifacts(task.id)) == 1
    assert (await store.get_artifact(task.id, "changes.patch")).size_bytes == 6
    with pytest.raises(NotFoundError):
        await store.get_artifact(task.id, "missing")
