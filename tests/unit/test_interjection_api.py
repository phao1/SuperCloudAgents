from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from fastapi import FastAPI

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.core.errors import ConflictError, NotFoundError
from sca.core.models import TaskMessage
from sca.storage.local import LocalObjectStore


def app_with_journal(tmp_path, monkeypatch, row, orchestrator) -> FastAPI:
    saved = []

    class Journal:
        sessionmaker = SimpleNamespace(kw={"bind": SimpleNamespace(url=SimpleNamespace(
            render_as_string=lambda **kw: "postgresql+asyncpg://localhost/sca",
        ))})

        def __init__(self, tenant_id="tenant-a"):
            self.tenant_id = tenant_id

        def for_tenant(self, tenant_id):
            return Journal(tenant_id)

        async def list_task_messages(self, task_id):
            if (task_id, self.tenant_id) != (row.id, "tenant-a"):
                raise NotFoundError("Task not found")
            return saved

        async def add_task_message(self, task_id, body, actor):
            await self.list_task_messages(task_id)
            if row.terminal:
                raise ConflictError("任务已结束")
            message = TaskMessage(
                id=uuid4(), task_id=task_id, seq=len(saved) + 1, **body.model_dump(),
                actor=actor, status="pending", created_at=datetime.now(UTC), turn=None, plan_attempt=None,
            )
            saved.append(message)
            return message

    settings = Settings(_env_file=None, data_dir=tmp_path)
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    return create_app(
        store=Journal(), orchestrator=orchestrator, object_store=LocalObjectStore(tmp_path),
        authenticator=StaticTokenAuthenticator({
            "member": "tenant-a:member", "viewer": "tenant-a:viewer", "other": "tenant-b:member",
        }),
    )


async def test_routes_with_scripted_store(tmp_path, monkeypatch):
    row = SimpleNamespace(id=uuid4(), terminal=False)
    orchestrator = SimpleNamespace(submit_task_message=AsyncMock())
    app = app_with_journal(tmp_path, monkeypatch, row, orchestrator)
    path = f"/v1/tasks/{row.id}/messages"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        for role, code in (("missing", 401), ("viewer", 403), ("other", 404)):
            response = await client.post(path, json={"text": "补充测试"},
                                         headers={"Authorization": "Bearer " + role})
            assert response.status_code == code
        member = {"Authorization": "Bearer member"}
        for text in ("\n  ", "\x00", "x" * 4001):
            assert (await client.post(path, json={"text": text}, headers=member)).status_code == 422
        response = await client.post(path, json={"text": "请补充测试"}, headers=member)
        assert response.status_code == 201
        sent = response.json()
        assert [sent["mode"], sent["status"], sent["actor"]] == ["queue", "pending", "static-token"]
        orchestrator.submit_task_message.assert_awaited_once_with(str(row.id), sent["id"], "queue")
        assert (await client.get(path, headers={"Authorization": "Bearer viewer"})).json() == [sent]
        assert (await client.get(path, headers={"Authorization": "Bearer other"})).status_code == 404
        row.terminal = True
        assert (await client.post(path, json={"text": "too late"}, headers=member)).status_code == 409
