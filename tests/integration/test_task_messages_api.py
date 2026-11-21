import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.core.models import TaskMessageInput, TaskState
from sca.db.schema import task_messages
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def postgres():
    try:
        with socket.create_connection(("127.0.0.1", 5432), .5):
            pass
    except OSError:
        pytest.skip("PostgreSQL is unreachable from the test sandbox")


async def test_message_routes_authorization_and_history(store, api_store, tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    orchestrator = SimpleNamespace(submit_task_message=AsyncMock())
    app = create_app(
        store=api_store,
        orchestrator=orchestrator,
        authenticator=StaticTokenAuthenticator({
            "member": "tenant-a:member", "viewer": "tenant-a:viewer", "other": "tenant-b:member",
        }),
        object_store=LocalObjectStore(tmp_path),
    )
    task, _ = await store.create_task(
        tenant_id="tenant-a", kind="coding", spec={}, idempotency_key=None,
    )
    path = f"/v1/tasks/{task.id}/messages"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        body = {"text": "请补充测试"}
        assert (await client.post(path, json=body)).status_code == 401
        viewer = {"Authorization": "Bearer viewer"}
        assert (await client.post(path, json=body, headers=viewer)).status_code == 403
        other = {"Authorization": "Bearer other"}
        assert (await client.get(path, headers=other)).status_code == 404
        assert (await client.post(path, json=body, headers=other)).status_code == 404
        member = {"Authorization": "Bearer member"}
        for invalid in ("  ", "x" * 4001, "a\x00b"):
            assert (await client.post(path, json={"text": invalid}, headers=member)).status_code == 422
        assert (await client.post(path, json={**body, "mode": "other"}, headers=member)).status_code == 422
        response = await client.post(path, json=body, headers=member)
        assert response.status_code == 201
        sent = response.json()
        assert sent["text"] == body["text"]
        assert sent["mode"] == "queue" and sent["status"] == "pending"
        assert sent["actor"] == "static-token"
        orchestrator.submit_task_message.assert_awaited_once_with(str(task.id), sent["id"], "queue")
        assert (await client.get(path, headers=viewer)).json() == [sent]

        delivered = await store.deliver_task_messages(str(task.id), 1)
        assert [str(message.id) for message in delivered] == [sent["id"]]
        assert (await client.get(path, headers=viewer)).json()[0]["status"] == "delivered"
        await store.apply_task_messages(str(task.id), [sent["id"]])
        assert (await client.get(path, headers=viewer)).json()[0]["status"] == "applied"
        await store.set_state(task.id, TaskState.CANCELLED)
        assert (await client.post(path, json=body, headers=member)).status_code == 409
        assert (await client.get(path, headers=viewer)).json()[0]["status"] == "applied"

    async with api_store.for_tenant("tenant-b")._transaction() as session:
        assert (await session.execute(select(task_messages))).first() is None


async def test_delivery_is_ordered_and_blocks_terminal_transition(store, api_store):
    task, _ = await store.create_task(
        tenant_id="tenant-a", kind="coding", spec={}, idempotency_key=None,
    )
    for state in (TaskState.PROVISIONING, TaskState.RUNNING, TaskState.VERIFYING,
                  TaskState.AWAITING_REVIEW):
        await store.set_state(task.id, state)
    tenant = api_store.for_tenant(task.tenant_id)
    messages = await asyncio.gather(*(
        tenant.add_task_message(task.id, TaskMessageInput(text=f"guidance {i}"), "actor")
        for i in range(3)
    ))
    ordered = sorted(messages, key=lambda message: message.seq)
    finished = await store.set_state(task.id, TaskState.SUCCEEDED, wait_for_messages=True)
    assert finished.state == TaskState.AWAITING_REVIEW
    for _ in range(2):
        delivered = await store.deliver_task_messages(str(task.id), 2)
        assert [message.id for message in delivered] == [message.id for message in ordered]
    assert (await store.set_state(task.id, TaskState.SUCCEEDED, wait_for_messages=True)).state == (
        TaskState.AWAITING_REVIEW
    )
    ids = [str(message.id) for message in ordered]
    for _ in range(2):
        await store.apply_task_messages(str(task.id), ids)
    assert (await store.set_state(task.id, TaskState.SUCCEEDED, wait_for_messages=True)).state == (
        TaskState.SUCCEEDED
    )
    events = await store.list_events(task.id)
    for type_ in ("user.message", "user.message.delivered", "user.message.applied"):
        assert [event.payload["message_id"] for event in events if event.type == type_] == ids
