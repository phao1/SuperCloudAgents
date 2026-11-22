from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Annotated
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from starlette.responses import Response

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.api.authz import GuestPrincipal, authorize
from sca.api.guest_history import history_dag
from sca.config import Settings
from sca.core.errors import AuthError, NotFoundError
from sca.core.models import (
    HISTORY_MISSING_ARTIFACT_URI,
    Artifact,
    Event,
    Principal,
    Task,
    TaskMessage,
    TaskState,
)
from sca.core.task_dag import DAGEdge, DAGNode, TaskDAG
from sca.evolve.solidify import SOLIDIFY_KIND
from sca.storage.local import LocalObjectStore, key_for


def auth_app(settings, *, deleted=False):
    app = FastAPI()
    app.state.settings = settings
    app.state.authenticator = StaticTokenAuthenticator({"fixture": "other:admin"})
    app.state.store = SimpleNamespace(tenant_deleted=AsyncMock(return_value=deleted))
    app.add_exception_handler(AuthError, lambda request, exc: Response(status_code=401))

    @app.api_route("/history", methods=["GET", "POST"])
    async def history(user: Annotated[Principal, Depends(authorize("read", guest=True))]) -> dict:
        return {**user.model_dump(), "guest": isinstance(user, GuestPrincipal)}

    return app


@pytest.mark.parametrize(
    ("mode", "peer", "forwarded", "status"),
    [
        ("off", "127.0.0.1", "127.0.0.1", 401),
        ("local", "127.1.2.3", "203.0.113.5", 200),
        ("local", "::1", "203.0.113.5", 200),
        ("local", "::ffff:127.0.0.1", "203.0.113.5", 200),
        ("local", "203.0.113.5", "127.0.0.1", 401),
        ("local", "localhost", "127.0.0.1", 401),
        ("all", "203.0.113.5", "203.0.113.5", 200),
    ],
)
async def test_guest_uses_the_socket_peer(mode, peer, forwarded, status):
    app = auth_app(Settings(_env_file=None, guest_view=mode, guest_tenant="history",
                            guest_view_acknowledge_risk="1"))
    transport = httpx.ASGITransport(app=app, client=(peer, 50123))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/history", headers={
            "X-Forwarded-For": forwarded, "Forwarded": f"for={forwarded}",
        })
        assert response.status_code == status
        if status == 200:
            assert response.json() == {
                "tenant_id": "history", "subject": "guest", "role": "viewer", "scopes": [],
                "repo_url": None, "guest": True,
            }
        assert (await client.post("/history")).status_code == 401


def test_guest_default_and_risk_acknowledgement(monkeypatch):
    monkeypatch.delenv("SCA_GUEST_VIEW", raising=False)
    monkeypatch.delenv("SCA_GUEST_VIEW_ACKNOWLEDGE_RISK", raising=False)
    assert Settings(_env_file=None).guest_view == "local"
    monkeypatch.setenv("SCA_GUEST_VIEW", "all")
    with pytest.raises(ValidationError, match="SCA_GUEST_VIEW_ACKNOWLEDGE_RISK=1") as error:
        Settings(_env_file=None, api_tokens="fixture-canary:default")
    assert "fixture-canary" not in str(error.value)
    monkeypatch.setenv("SCA_GUEST_VIEW_ACKNOWLEDGE_RISK", "true")
    with pytest.raises(ValidationError, match="SCA_GUEST_VIEW_ACKNOWLEDGE_RISK=1"):
        Settings(_env_file=None)
    monkeypatch.setenv("SCA_GUEST_VIEW_ACKNOWLEDGE_RISK", "1")
    assert Settings(_env_file=None).guest_view == "all"


async def test_guest_fallback_respects_tokens_and_deleted_tenants(monkeypatch):
    monkeypatch.delenv("SCA_PORTAL_LOCAL_CONFIG", raising=False)
    monkeypatch.delenv("SCA_RUNTIME_MODE", raising=False)
    settings = Settings(_env_file=None, guest_view="local", guest_tenant=None)
    app = auth_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/history")).json()["tenant_id"] == "default"
        for header in ("Bearer bad", "Bearer ", "", "Basic fixture"):
            assert (await client.get("/history", headers={"Authorization": header})).status_code == 401
        response = await client.get("/history", headers={"Authorization": "Bearer fixture"})
        assert response.json()["tenant_id"] == "other"
        assert response.json()["guest"] is False
        app.state.store.tenant_deleted.return_value = True
        assert (await client.get("/history")).status_code == 401


@pytest.mark.parametrize("mode", ["docker", "source"])
async def test_guest_tenant_defaults_to_eval_in_legacy_demo(monkeypatch, mode):
    monkeypatch.delenv("SCA_PORTAL_LOCAL_CONFIG", raising=False)
    monkeypatch.delenv("SCA_GUEST_TENANT", raising=False)
    monkeypatch.setenv("SCA_RUNTIME_MODE", mode)
    app = auth_app(Settings(_env_file=None, guest_view="local"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/history")).json()["tenant_id"] == "eval"
        monkeypatch.setenv("SCA_GUEST_TENANT", "custom")
        app.state.settings = Settings(_env_file=None, guest_view="local")
        assert (await client.get("/history")).json()["tenant_id"] == "custom"


@pytest.fixture
async def demo_api(monkeypatch, tmp_path):
    config = tmp_path / "provider.env"
    config.write_text("DEEPSEEK_API_KEY=REPLACE_ME\n")
    monkeypatch.setenv("SCA_PORTAL_LOCAL_CONFIG", str(config))
    settings = Settings(_env_file=None, data_dir=tmp_path, guest_view="local", guest_tenant=None)
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    now, task_id, other_id = datetime.now(UTC), uuid4(), uuid4()
    task = Task(id=task_id, tenant_id="eval", kind="coding", state=TaskState.CANCELLED,
                spec={"instruction": "fixture", "repo_url": "https://user:fixture@repo.example/test",
                      "intents": [{"params": {"tokenSecret": "fixture-reference", "issue": "101"}}]},
                result={"stripe_customer_id": "cus_fixture123", "owner_email": "owner@example.org"},
                usage={"input_tokens": 2}, idempotency_key="fixture-retry", created_at=now, updated_at=now)
    event = Event(task_id=task_id, seq=1, type="agent.message", created_at=now,
                  payload={"text": "message for owner@example.org", "api_key": "fixture-event"})
    patch = b"+answer = 42\n+DEEPSEEK_API_KEY=fixture-patch\n+contact = 'owner@example.org'\n"
    artifacts = [Artifact(task_id=task_id, name="changes.patch", content_type="text/x-diff",
                          uri="s3://fixture-private/changes.patch", size_bytes=len(patch), created_at=now),
                 Artifact(task_id=task_id, name="changes.bundle", content_type="application/octet-stream",
                          uri="s3://fixture-private/changes.bundle", size_bytes=5, created_at=now)]

    async def get_task(task_id, tenant_id):
        if task_id != task.id or tenant_id != task.tenant_id:
            raise NotFoundError("Task not found")
        return task

    scoped = SimpleNamespace(
        list_tasks=AsyncMock(return_value=[task]), get_task=get_task,
        list_events=AsyncMock(return_value=[event]), list_task_children=AsyncMock(return_value=[]),
        list_artifacts=AsyncMock(return_value=artifacts),
        get_artifact=AsyncMock(side_effect=lambda _, name: next(a for a in artifacts if a.name == name)),
        get_eval_run=AsyncMock(return_value={"variant": {"api_key": "fixture-eval"}, "results": [],
                                            "created_by": "owner@example.org"}),
        list_evolutions=AsyncMock(return_value=[{"status": "finished", "created_by": "owner@example.org"}]),
        get_evolution=AsyncMock(return_value={"status": "finished", "config": {"credential": "fixture-evo"}}),
        usage_rows=AsyncMock(return_value=[{"tenant_id": "eval", "tokens_in": 2, "tasks": 1}]),
        create_task=AsyncMock(), create_eval_run=AsyncMock(), create_evolution=AsyncMock(),
        add_task_message=AsyncMock(),
    )
    store = SimpleNamespace(
        for_tenant=Mock(side_effect={"eval": scoped}.__getitem__), tenant_deleted=AsyncMock(return_value=False),
        sessionmaker=SimpleNamespace(kw={"bind": SimpleNamespace(url=make_url("postgresql+asyncpg://localhost/sca"))}),
    )
    objects = LocalObjectStore(tmp_path / "objects")
    await objects.put(key_for(task_id, "changes.patch"), patch)
    app = create_app(store=store, object_store=objects, orchestrator=SimpleNamespace(),
                     authenticator=StaticTokenAuthenticator({"fixture": "eval:admin", "viewer": "eval:viewer"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, scoped, task_id, other_id


async def test_guest_history_and_credential_fields(demo_api):
    client, store, task_id, other_id = demo_api
    assert (await client.get("/v1/portal/config")).json() == {
        "guest_view": True, "guest_tenant": "eval", "model_configured": False,
    }
    tasks = await client.get("/v1/tasks?tenant_id=other")
    store.list_tasks.assert_awaited_once_with("eval", None, 50)
    assert [task["id"] for task in tasks.json()] == [str(task_id)]
    assert (await client.get(f"/v1/tasks/{other_id}/events")).status_code == 404
    root = f"/v1/tasks/{task_id}"
    task = (await client.get(root)).json()
    assert task == tasks.json()[0]
    assert task["idempotency_key"] is None
    assert task["spec"]["intents"] == [{"params": {"issue": "101"}}]
    assert task["usage"]["input_tokens"] == 2
    assert "user:fixture" not in task["spec"]["repo_url"]
    assert task["result"] == {}
    events = await client.get(root + "/events")
    assert events.json()[0]["payload"] == {"text": "message for [redacted]"}
    stream = await client.get(root + "/events/stream")
    assert stream.status_code == 200 and "event: agent.message" in stream.text
    assert "fixture-event" not in stream.text and "owner@example.org" not in stream.text
    artifacts = (await client.get(root + "/artifacts")).json()
    assert [item["uri"] for item in artifacts] == [root + "/artifacts/changes.patch"]
    patch = await client.get(artifacts[0]["uri"])
    assert patch.status_code == 200 and "+answer = 42" in patch.text
    assert "fixture-patch" not in patch.text and "owner@example.org" not in patch.text
    assert (await client.get(root + "/artifacts/changes.bundle")).status_code == 403
    assert (await client.get(f"/v1/evals/runs/{uuid4()}")).json()["variant"] == {}
    assert (await client.get("/v1/evolutions")).json() == [{"status": "finished"}]
    assert (await client.get(f"/v1/evolutions/{uuid4()}")).json()["config"] == {}
    assert (await client.get("/v1/usage?tenant_id=other")).json() == [
        {"tenant_id": "eval", "tokens_in": 2, "tasks": 1},
    ]

    auth = {"Authorization": "Bearer fixture"}
    assert (await client.get(root, headers=auth)).json()["idempotency_key"] == "fixture-retry"
    assert "fixture-patch" in (await client.get(artifacts[0]["uri"], headers=auth)).text
    assert (await client.get("/v1/evolutions", headers={"Authorization": "Bearer viewer"})).status_code == 403


async def test_missing_artifact_is_visible_without_bypassing_guest_download_rules(demo_api):
    client, store, task_id, _ = demo_api
    artifacts = store.list_artifacts.return_value
    artifacts[:] = [a.model_copy(update={"uri": HISTORY_MISSING_ARTIFACT_URI}) for a in artifacts]
    path = f"/v1/tasks/{task_id}/artifacts"
    reason = "文件缺失（源机器上已清理）"
    for headers in ({}, {"Authorization": "Bearer fixture"}):
        response = await client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.json()[0]["missing_reason"] == reason
        gone = await client.get(path + "/changes.patch", headers=headers)
        assert gone.status_code == 410
        assert gone.json() == {"error": {"code": "artifact_missing", "message": reason}}
    assert [item["name"] for item in (await client.get(path)).json()] == ["changes.patch"]
    assert (await client.get(path + "/changes.bundle")).status_code == 403


async def test_guest_evolution_dag_champions_and_write_permissions(demo_api, monkeypatch):
    client, store, task_id, _ = demo_api
    evolution_id, candidate_id, champion_id, run_id = (uuid4() for _ in range(4))
    now = datetime.now(UTC)
    store.get_evolution_dag_history = AsyncMock(return_value={
        "evolution": {"id": evolution_id, "status": "running", "spent_usd": 0, "created_at": now,
                      "finished_at": None, "config": {"reason": "Review with owner@example.org"}},
        "candidates": [{"id": candidate_id, "generation": 1, "decision": "pending", "created_at": now,
                        "hypothesis": "Ping owner@example.org", "overlay": {}, "dev_run_id": run_id}],
        "runs": [{"id": run_id, "status": "running", "variant": {"split": "dev"},
                  "created_at": now, "finished_at": None}],
        "results": [], "champions": [],
        "tasks": [{"id": task_id, "state": "RUNNING", "created_at": now, "updated_at": now,
                   "eval_run_id": str(run_id), "idempotency_key": f"eval:{run_id}:owner@example.org:0"}],
    })
    champion = {"id": str(champion_id), "created_by": "owner@example.org", "overlay": {
        "tool_guidance": "Review with owner@example.org", "api_key": "fixture-overlay",
    }}
    store.get_champion = AsyncMock(return_value=champion)
    store.list_champions = AsyncMock(return_value=[champion])
    build = {"id": str(uuid4()), "labels": {"kind": SOLIDIFY_KIND, "champion_id": str(champion_id)},
             "status": "succeeded", "log_tail": "fixture-build-log", "created_by": "owner@example.org"}
    monkeypatch.setattr("sca.api.agents.builds_list", AsyncMock(return_value=[build]))

    evolution = f"/v1/evolutions/{evolution_id}"
    champion_path = f"/v1/champions/{champion_id}"
    guest_graph = await client.get(evolution + "/dag", params={"tenant_id": "other"})
    assert guest_graph.status_code == 200 and "owner@example.org" not in guest_graph.text
    nodes = {node["kind"]: node for node in guest_graph.json()["nodes"]}
    assert nodes["candidate"]["meta"]["hypothesis"] == "Ping [redacted]"
    assert nodes["case_task"]["task_id"] == str(task_id) and nodes["case_task"]["meta"]["parent"] == f"eval:{run_id}"
    assert {edge["from"] for edge in guest_graph.json()["edges"]} >= {f"eval:{run_id}"}
    store.get_evolution_dag_history.assert_awaited_once_with(evolution_id)

    for path in ("/v1/champions", champion_path):
        response = await client.get(path)
        assert response.status_code == 200
        assert all(value not in response.text for value in ("created_by", "owner@example.org", "fixture-overlay"))
    assert (await client.get(champion_path)).json()["solidify_builds"] == [{"id": build["id"], "status": "succeeded"}]
    admin = {"Authorization": "Bearer fixture"}
    assert "owner@example.org" in (await client.get(evolution + "/dag", headers=admin)).text
    assert (await client.get(champion_path, headers=admin)).json()["solidify_builds"] == [build]

    viewer = {"Authorization": "Bearer viewer"}
    for path in (evolution + "/dag", "/v1/champions", champion_path):
        assert (await client.get(path, headers=viewer)).status_code == 403
        assert (await client.get(path, headers={"Authorization": "Bearer invalid"})).status_code == 401
    for method, path in [
        ("POST", "/v1/evolutions"), ("POST", evolution + "/stop"), ("POST", evolution + "/approve"),
        ("POST", evolution + "/reject"), ("POST", champion_path + "/activate"),
        ("POST", champion_path + "/solidify"), ("POST", champion_path + "/rollback"),
        ("GET", "/v1/champions/rollouts"), ("PUT", "/v1/champions/rollouts"),
        ("GET", "/v1/evolution-schedules"), ("POST", "/v1/evolution-schedules"),
        ("PUT", f"/v1/evolution-schedules/{candidate_id}"), ("DELETE", f"/v1/evolution-schedules/{candidate_id}"),
        ("POST", f"/v1/evolution-schedules/{candidate_id}/run-now"),
    ]:
        assert (await client.request(method, path, json={})).status_code == 401, (method, path)
        assert (await client.request(method, path, json={}, headers=viewer)).status_code == 403, (method, path)


async def test_guest_dag_and_message_history(demo_api):
    client, store, task_id, other_id = demo_api
    task = await store.get_task(task_id, "eval")
    names = ["member@example.org", "reviewer@example.org"]
    task.spec["subtasks"] = [{"name": name} for name in names]
    raw_text = (
        "Review for member@example.org\nTOKEN=fixture-message\nInvoice in_fixture123\n"
        "https://ci.example.org/job?access_token=fixture-query"
    )
    guest_text = (
        "Review for [redacted]\nTOKEN=[redacted]\nInvoice [redacted]\n"
        "https://ci.example.org/job?access_token=[redacted]"
    )
    message = TaskMessage(
        id=uuid4(), task_id=task_id, seq=1, text=raw_text, mode="interrupt", status="pending", actor="fixture-subject",
        created_at=task.created_at, turn=None, plan_attempt=None,
    )
    store.list_events.return_value = [Event(
        task_id=task_id, seq=1, type="user.message", created_at=task.created_at,
        payload={"message_id": str(message.id), "text": message.text, "mode": message.mode, "actor": message.actor},
    )]

    async def list_messages(id_):
        await store.get_task(id_, "eval")
        return [message]

    store.list_task_messages = list_messages
    path = f"/v1/tasks/{task_id}"
    auth = {"Authorization": "Bearer viewer"}
    response = await client.get(path + "/messages")
    assert response.status_code == 200
    assert response.json() == [{**message.model_dump(mode="json"), "actor": "[redacted]", "text": guest_text}]
    assert (await client.get(path + "/messages", headers=auth)).json() == [message.model_dump(mode="json")]
    assert (await client.post(path + "/messages", json={"text": "Follow up"})).status_code == 401
    store.add_task_message.assert_not_called()

    response = await client.get(path + "/dag")
    assert response.status_code == 200
    graph = response.json()
    ids = {node["id"] for node in graph["nodes"]}
    assert len(ids) == len(graph["nodes"])
    assert all(edge["from"] in ids and edge["to"] in ids for edge in graph["edges"])
    assert [node["label"] for node in graph["nodes"] if node["kind"] == "subtask"] == ["[redacted]"] * 2
    interjection = next(node for node in graph["nodes"] if node["kind"] == "interjection")
    assert interjection["meta"]["text"] == guest_text
    assert "actor" not in interjection["meta"]
    assert all(name not in response.text for name in names)
    response = await client.get(path + "/dag", headers=auth)
    assert all(name in response.text for name in names)
    assert message.actor in response.text and "fixture-message" in response.text
    for suffix in ("/dag", "/messages"):
        assert (await client.get(f"/v1/tasks/{other_id}" + suffix)).status_code == 404
        assert (await client.get(path + suffix, headers={"Authorization": "Bearer invalid"})).status_code == 401


def test_guest_dag_preserves_redacted_parent_links():
    task_id = uuid4()
    parent = DAGNode(id=f"task:{task_id}/subtask:member@example.org", label="member@example.org",
                     kind="subtask", task_id=task_id)
    child = DAGNode(id=f"{parent.id}/turn:1", kind="turn", label="Execute", task_id=task_id,
                    meta={"parent": parent.id})
    graph = history_dag(GuestPrincipal(tenant_id="eval"), TaskDAG(
        nodes=[parent, child], edges=[DAGEdge(source=parent.id, target=child.id, kind="spawn")],
    ))
    assert graph.nodes[1].meta["parent"] == graph.edges[0].source == graph.nodes[0].id
    assert graph.edges[0].target == graph.nodes[1].id
    assert "member@example.org" not in graph.model_dump_json()


async def test_guest_project_reads_and_writes_require_a_token(demo_api):
    client, _, _, _ = demo_api
    path = f"/v1/projects/{uuid4()}"
    for url in ("/v1/projects", path, path + "/threads", path + "/usage", path + "/library"):
        assert (await client.get(url)).status_code == 401
    writes = [
        ("POST", "/v1/projects", {"name": "New"}), ("PATCH", path, {"name": "Edited"}),
        ("DELETE", path, None),
    ]
    writes.extend(("POST", f"{path}/{action}", None) for action in ("pause", "resume", "archive", "unarchive"))
    for method, url, body in writes:
        assert (await client.request(method, url, json=body)).status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/tasks"), ("POST", "/v1/evals/runs"), ("POST", "/v1/evolutions"),
        ("POST", "/v1/portal/provider-key"), ("DELETE", "/v1/admin/tenants/other"),
        ("GET", "/v1/tenant/members"), ("GET", "/v1/tenant/billing"),
        ("GET", "/v1/integrations/scm"), ("GET", "/v1/tokens"), ("GET", "/v1/admin/usage"),
        ("GET", "/v1/knowledge/documents"),
    ],
)
async def test_guest_cannot_use_other_routes(demo_api, method, path):
    client, _, _, _ = demo_api
    assert (await client.request(method, path, json={})).status_code == 401


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/tasks", {"kind": "coding", "spec": {"repo_url": "/repo", "instruction": "check"}}),
        ("/v1/evals/runs", {"suite": "suite", "budget": 1, "case_budget": 0.1}),
        ("/v1/evolutions", {"suite": "suite", "agent_version": "2.3.0-sca.0"}),
    ],
)
async def test_keyless_demo_rejects_submissions_before_creating_records(demo_api, path, body):
    client, store, _, _ = demo_api
    response = await client.post(path, json=body, headers={"Authorization": "Bearer fixture"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "model_not_configured"
    store.create_task.assert_not_called()
    store.create_eval_run.assert_not_called()
    store.create_evolution.assert_not_called()
