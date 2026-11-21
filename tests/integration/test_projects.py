from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.core.models import TaskState
from sca.db.schema import projects, tasks
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


@pytest.fixture
async def api(api_store, tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    monkeypatch.setattr("sca.api.knowledge.get_settings", lambda: settings)
    orchestrator = SimpleNamespace(start_task=AsyncMock(), cancel_task=AsyncMock())
    app = create_app(
        store=api_store, orchestrator=orchestrator, object_store=LocalObjectStore(tmp_path),
        authenticator=StaticTokenAuthenticator({
            "admin": "acme", "member": "acme:member",
            "viewer": "acme:viewer", "outsider": "another",
        }),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        async def request(method, path, role="admin", *, headers=None, **kwargs):
            return await client.request(method, path, headers={
                "Authorization": "Bearer " + role, **(headers or {}),
            }, **kwargs)

        yield request, orchestrator


async def new_project(request, name="项目", **fields):
    response = await request("POST", "/v1/projects", "member", json={"name": name, **fields})
    assert response.status_code == 201, response.text
    return response.json()


async def submit(request, project_id=None, **spec):
    return await request("POST", "/v1/tasks", "member", json={
        "kind": "coding", "project_id": project_id, "spec": {"instruction": "补充测试", **spec},
    })


async def test_migration_backfills_tasks_one_default_per_tenant(store):
    migration = import_module("sca.db.migrations.versions.0024_projects")

    def migrate(connection, direction):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, direction)()

    async with store._transaction() as session:
        connection = await session.connection()
        await connection.run_sync(migrate, "downgrade")
        await session.execute(text("""INSERT INTO tenants (id, display_name, max_active_tasks)
            VALUES ('empty', 'empty', 20)"""))
        await session.execute(text("""INSERT INTO tasks (id, tenant_id, kind, spec, state)
            SELECT gen_random_uuid(), tenant, 'coding', '{}', 'QUEUED'
            FROM (VALUES ('first'), ('first'), ('second')) AS x(tenant)"""))
        await connection.run_sync(migrate, "upgrade")
        rows = (await session.execute(select(
            tasks.c.tenant_id, tasks.c.project_id, projects.c.name, projects.c.is_default,
        ).join(projects, projects.c.id == tasks.c.project_id))).all()
        assert len(rows) == 3
        assert {(tenant, name, default) for tenant, _, name, default in rows} == {
            ("first", "默认项目", True), ("second", "默认项目", True),
        }
        assert len({project for tenant, project, _, _ in rows if tenant == "first"}) == 1
        assert await session.scalar(select(projects.c.id).where(projects.c.tenant_id == "empty")) is None


async def test_projects_rls_and_foreign_task_assignment(api, api_store):
    request, _ = api
    project = await new_project(request)
    project_id = UUID(project["id"])
    foreign = (await request("POST", "/v1/projects", "outsider", json={"name": "另一个租户"})).json()
    for suffix in ("", "/threads", "/usage", "/library"):
        assert (await request("GET", f"/v1/projects/{foreign['id']}{suffix}")).status_code == 404
    assert (await submit(request, foreign["id"], repo_url="/repo")).status_code == 404
    own_task = (await submit(request, project["id"], repo_url="/repo")).json()

    async with api_store.for_tenant("another")._transaction() as session:
        assert list(await session.scalars(select(projects.c.id))) == [UUID(foreign["id"])]
        assert (await session.execute(update(projects).where(projects.c.id == project_id)
                                     .values(name="覆盖"))).rowcount == 0
    with pytest.raises(DBAPIError):
        async with api_store.for_tenant("another")._transaction() as session:
            await session.execute(insert(projects).values(
                id=uuid4(), tenant_id="acme", name="注入", created_by="fixture",
            ))
    with pytest.raises(IntegrityError):
        async with api_store.for_tenant("acme")._transaction() as session:
            await session.execute(update(tasks).where(tasks.c.id == UUID(own_task["id"]))
                                  .values(project_id=UUID(foreign["id"])))


async def test_project_crud_lifecycle_and_lazy_default(api, store):
    request, orchestrator = api
    assert (await request("GET", "/v1/projects", "viewer")).json() == []
    assert (await request("POST", "/v1/projects", "viewer", json={"name": "A"})).status_code == 403
    assert (await request("POST", "/v1/projects", json={
        "name": "A", "instructions": "字" * 16001,
    })).status_code == 422
    project = await new_project(request, goal="保留现有行为", repos=[{"repo_url": "/repo"}])
    url = "/v1/projects/" + project["id"]
    assert (await request("GET", url, "viewer")).json() == project
    assert (await request("PATCH", url, "viewer", json={"goal": "新目标"})).status_code == 403
    changed = await request("PATCH", url, "member", json={"goal": "新目标", "settings": {"ticket": "A"}})
    assert changed.json()["goal"] == "新目标"
    assert changed.json()["settings"] == {"ticket": "A"}
    assert (await request("DELETE", url, "member")).status_code == 403
    assert (await request("POST", url + "/archive", "viewer")).status_code == 403
    first = (await submit(request, project["id"])).json()
    assert (await request("DELETE", url)).status_code == 409
    paused = await request("POST", url + "/pause", "member")
    assert paused.json()["status"] == "paused"
    assert (await submit(request, project["id"])).status_code == 409
    assert (await store.acquire_slot(first["id"], 5))["project_blocked"]
    assert (await request("POST", url + "/resume")).json()["status"] == "active"
    assert (await store.acquire_slot(first["id"], 5))["granted"]
    assert (await request("POST", url + "/archive")).json()["status"] == "archived"
    assert (await submit(request, project["id"])).status_code == 409
    assert (await request("POST", url + "/resume")).status_code == 409
    assert (await request("POST", url + "/unarchive")).json()["status"] == "active"
    await store.set_state(first["id"], TaskState.CANCELLED)
    assert (await request("DELETE", url)).status_code == 204
    assert (await request("GET", url)).status_code == 404
    assert (await request("GET", "/v1/tasks/" + first["id"])).json()["project_id"] == project["id"]

    defaults = await submit(request, repo_url="/legacy")
    again = await submit(request, repo_url="/legacy")
    assert defaults.status_code == again.status_code == 201
    assert defaults.json()["project_id"] == again.json()["project_id"]
    only, = (await request("GET", "/v1/projects")).json()
    assert (only["name"], only["is_default"], only["group_counts"]["working"]) == ("默认项目", True, 2)
    assert orchestrator.start_task.await_count == 3


async def test_thread_defaults_instruction_snapshot_and_groups(api, store):
    request, _ = api
    project = await new_project(
        request, instructions="使用项目约定", default_backend="fake", default_model="model-a",
        repos=[{"repo_url": "/repo", "base_ref": "develop"}, {"repo_url": "/second", "base_ref": "next"}],
    )
    url = "/v1/projects/" + project["id"]
    thread = (await submit(request, project["id"])).json()
    first = thread["spec"]
    assert first["instruction"] == "## Project instructions\n\n使用项目约定\n\n## Thread request\n\n补充测试"
    assert (first["repo_url"], first["base_ref"], first["agent_backend"], first["model"]) == (
        "/repo", "develop", "fake", "model-a",
    )
    await request("PATCH", url, json={"instructions": "新的约定", "default_model": "model-b"})
    override = (await submit(request, project["id"], repo_url="/second", model="alternate")).json()
    assert override["spec"]["base_ref"] == "next"
    assert override["spec"]["model"] == "alternate"
    assert "新的约定" in override["spec"]["instruction"]
    assert (await store.get_task(thread["id"])).spec == first
    await store.set_state(thread["id"], TaskState.PROVISIONING)
    await store.set_state(thread["id"], TaskState.RUNNING)
    await store.set_state(thread["id"], TaskState.VERIFYING)
    await store.append_event(thread["id"], "ci.requested", {"turn": 1})
    landing, = (await request("GET", url + "/threads?group=landing", "viewer")).json()
    assert landing["id"] == thread["id"]
    await store.append_event(thread["id"], "ci.completed", {"turn": 1})
    await store.append_event(thread["id"], "review.requested", {"plan": {"split": True}})
    await store.set_state(thread["id"], TaskState.AWAITING_REVIEW)
    waiting, = (await request("GET", url + "/threads?group=waiting_on_you")).json()
    assert waiting["id"] == thread["id"]
    await store.append_event(thread["id"], "review.requested", {"commit": "1234567"})
    ready, = (await request("GET", url + "/threads?group=ready_for_review")).json()
    assert ready["id"] == thread["id"]
    await store.set_state(thread["id"], TaskState.SUCCEEDED)
    states = {item["id"]: item["group"] for item in (await request("GET", url + "/threads")).json()}
    assert states == {thread["id"]: "resolved", override["id"]: "working"}


async def test_project_library_usage_and_memories(api, store, api_store):
    request, _ = api
    one, two = await new_project(request, name="一"), await new_project(request, name="二")
    thread = (await submit(request, one["id"], repo_url="/repo", model="model-a")).json()
    other = (await submit(request, two["id"], repo_url="/repo")).json()
    await store.add_artifact(thread["id"], "report.txt", "text/plain", "fixture:1", 42)
    await store.add_artifact(other["id"], "other.txt", "text/plain", "fixture:2", 56)
    await store.append_event(thread["id"], "agent.turn_started", {"turn": 1, "model": "model-a"})
    await store.append_event(thread["id"], "agent.usage", {"turn": 1, "input_tokens": 101})
    await store.append_event(thread["id"], "agent.usage", {"turn": 1, "model": "model-b", "output_tokens": 21})
    for title, project_id in (("tenant", None), ("one", one["id"]), ("two", two["id"])):
        assert (await request("POST", "/v1/knowledge/documents", json={
            "title": title, "text": "共享词汇知识文档", "project_id": project_id,
        })).status_code == 201
        assert (await request("POST", "/v1/memories", json={
            "text": f"记忆 {title}", "scope": "tenant", "project_id": project_id,
        })).status_code == 201
    path = "/v1/projects/" + one["id"]
    library = (await request("GET", path + "/library", "viewer")).json()
    assert {row["title"] for row in library["documents"]} == {"tenant", "one"}
    assert [row["name"] for row in library["artifacts"]] == ["report.txt"]
    hits = (await request("POST", "/v1/knowledge/search", "viewer", json={
        "query": "共享词汇", "project_id": one["id"],
    })).json()
    assert {row["title"] for row in hits} == {"tenant", "one"}
    usage = (await request("GET", path + "/usage", "viewer")).json()
    assert usage["totals"]["tokens_in"] == 101 and usage["totals"]["tokens_out"] == 21
    assert usage["by_model"]["model-b"]["tokens_out"] == 21
    assert [row["task_id"] for row in usage["by_thread"]] == [thread["id"]]
    tenant = api_store.for_tenant("acme")
    memory = await tenant.inject_memories(thread["id"], 30, 3000)
    assert "记忆 one" in memory and "记忆 tenant" in memory and "记忆 two" not in memory
    proposals = await tenant.propose_memories(thread["id"], "agent_proposal", [{"text": "新建议"}])
    own = await tenant.list_memories(status="proposed", project_id=UUID(one["id"]))
    assert [str(row["id"]) for row in own] == proposals
    assert await tenant.list_memories(status="proposed", project_id=UUID(two["id"])) == []
