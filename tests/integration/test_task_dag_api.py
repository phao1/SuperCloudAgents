import socket
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.engine import make_url

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.archive import archive_task
from sca.config import Settings
from sca.core.errors import NotFoundError
from sca.core.models import Event, Principal, Task, TaskState
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration
VIEWER = {"Authorization": "Bearer viewer"}
OTHER = {"Authorization": "Bearer other"}
NOW = datetime(2026, 11, 21, tzinfo=UTC)


def app_client(store, tmp_path, monkeypatch, *, objects=None):
    class Authenticator(StaticTokenAuthenticator):
        async def authenticate(self, token):
            if token == "ci-only":
                return Principal(tenant_id="tenant-a", subject="ci", scopes=["ci:report"])
            return await super().authenticate(token)

    settings = Settings(_env_file=None, data_dir=tmp_path)
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    app = create_app(
        store=store, orchestrator=SimpleNamespace(),
        authenticator=Authenticator({"viewer": "tenant-a:viewer", "other": "tenant-b:member"}),
        object_store=objects or LocalObjectStore(tmp_path),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class MemoryHistory:
    def __init__(self, tasks, events, tenant=None):
        self.tasks = {task.id: task for task in tasks}
        self.events = events
        self.tenant = tenant
        self.sessionmaker = SimpleNamespace(kw={"bind": SimpleNamespace(
            url=make_url("postgresql+asyncpg://localhost/sca"),
        )})

    def for_tenant(self, tenant_id):
        return MemoryHistory(self.tasks.values(), self.events, tenant_id)

    async def get_task(self, task_id, tenant_id):
        task = self.tasks.get(task_id)
        if not task or task.tenant_id != self.tenant or tenant_id != self.tenant:
            raise NotFoundError("Task not found")
        return task

    async def list_task_children(self, task_id):
        return [task for task in self.tasks.values() if task.tenant_id == self.tenant
                and task.result.get("parent_task_id") == str(task_id)]

    async def list_events(self, task_id, after, limit):
        await self.get_task(task_id, self.tenant)
        return [e for e in self.events if e.task_id == task_id and e.seq > after][:limit]


def make_task(tenant, **fields):
    return Task(
        id=uuid4(), tenant_id=tenant, state=TaskState.RUNNING, kind="coding",
        spec=fields.pop("spec", {}), created_at=NOW, updated_at=NOW, **fields,
    )


async def check_dag(client, root, child, grandchild, other):
    path = f"/v1/tasks/{root.id}/dag"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=OTHER)).status_code == 404
    assert (await client.get(path, headers={"Authorization": "Bearer ci-only"})).status_code == 403
    response = await client.get(path, headers=VIEWER)
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"nodes", "edges"}
    assert {node["task_id"] for node in data["nodes"]} == {str(t.id) for t in (root, child, grandchild)}
    assert str(other.id) not in response.text
    required = {
        "id", "kind", "label", "status", "started_at", "finished_at",
        "duration_s", "cost_usd", "tokens", "task_id", "meta",
    }
    assert all(set(node) == required for node in data["nodes"])
    assert all(set(edge) == {"from", "to", "kind"} for edge in data["edges"])
    assert sum(edge["kind"] == "spawn" for edge in data["edges"]) >= 3
    assert any(node["kind"] == "turn" and node["task_id"] == str(grandchild.id) for node in data["nodes"])
    assert any(node["status"] == "pending" and node["label"] == "later" for node in data["nodes"])
    branch, = [node for node in data["nodes"] if node["meta"].get("child_task_id") == str(child.id)]
    assert next(node for node in data["nodes"] if node["id"] == f"task:{child.id}")["meta"]["parent"] == branch["id"]
    assert next(node for node in data["nodes"] if node["kind"] == "turn")["meta"]["parent"] == f"task:{grandchild.id}"


async def test_task_dag_http_contract_and_portal_assets_with_fake_store(tmp_path, monkeypatch):
    root = make_task("tenant-a", spec={"subtasks": [{"name": "report"}, {"name": "later"}]})
    child = make_task("tenant-a", result={"parent_task_id": str(root.id), "subtask": "report"})
    grandchild = make_task("tenant-a")
    other = make_task("tenant-b", result={"parent_task_id": str(root.id), "subtask": "private"})
    events = [
        Event(task_id=child.id, seq=1, type="task.child_started",
              payload={"child_task_id": str(grandchild.id)}, created_at=NOW),
        Event(task_id=root.id, seq=1, type="task.child_started",
              payload={"child_task_id": str(other.id)}, created_at=NOW),
        Event(task_id=grandchild.id, seq=1, type="agent.turn_started",
              payload={"turn": 1}, created_at=NOW),
    ]
    async with app_client(MemoryHistory([root, child, grandchild, other], events), tmp_path, monkeypatch) as client:
        await check_dag(client, root, child, grandchild, other)
        for name, media in [("task-dag.js", "text/javascript"), ("task-dag.css", "text/css")]:
            response = await client.get("/portal/assets/" + name)
            assert response.status_code == 200 and response.headers["content-type"].startswith(media)


@pytest.fixture
def postgres():
    try:
        with socket.create_connection(("127.0.0.1", 5432), .5):
            pass
    except OSError as exc:
        pytest.skip(f"PostgreSQL is unavailable to the sandbox ({exc.strerror})")


async def test_dag_tenant_scoping_and_archived_descendant(postgres, store, api_store, tmp_path, monkeypatch):
    async def create(tenant="tenant-a", *, spec=None, result=None):
        task, _ = await store.create_task(
            tenant_id=tenant, kind="coding", spec=spec or {}, idempotency_key=None,
            initial_result=result,
        )
        return task

    root = await create(spec={"subtasks": [{"name": "report"}, {"name": "later"}]})
    child = await create(result={"parent_task_id": str(root.id), "subtask": "report"})
    grandchild = await create()
    other = await create("tenant-b", result={"parent_task_id": str(root.id)})
    await store.append_event(child.id, "task.child_started", {"child_task_id": str(grandchild.id)})
    await store.append_event(root.id, "task.child_started", {"child_task_id": str(other.id)})
    await store.append_event(grandchild.id, "agent.turn_started", {"turn": 1})
    await store.set_state(grandchild.id, TaskState.CANCELLED)
    objects = LocalObjectStore(tmp_path / "objects")
    await archive_task(store, objects, grandchild.id)

    async with app_client(api_store, tmp_path, monkeypatch, objects=objects) as client:
        await check_dag(client, root, child, grandchild, other)
