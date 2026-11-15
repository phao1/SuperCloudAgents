import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from test_api import FakeOrchestrator, awaiting

from sca.api.app import create_app
from sca.api.auth import ChainAuthenticator, DatabaseTokenAuthenticator, StaticTokenAuthenticator
from sca.core.models import TaskState

pytestmark = pytest.mark.integration


@pytest.fixture
async def auth_api(store, api_store):
    app = create_app(
        store=api_store,
        orchestrator=FakeOrchestrator(store),
        authenticator=ChainAuthenticator(
            [
                StaticTokenAuthenticator({r: "a:" + r for r in ("viewer", "member", "admin")}),
                DatabaseTokenAuthenticator(api_store),
            ]
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer admin"},
    ) as client:
        yield client


WRITE_ROUTES = [
    (
        "POST",
        "/tasks",
        {"kind": "coding", "spec": {"repo_url": "/tmp/repo", "instruction": "fix"}},
        "member",
    ),
    ("POST", "/tasks/{id}/review", {"decision": "approve"}, "member"),
    ("POST", "/tasks/{id}/cancel", None, "member"),
    ("POST", "/tasks/{id}/pause", None, "member"),
    ("POST", "/tasks/{id}/resume", None, "member"),
    ("POST", "/tasks/{id}/ci-result", {"commit": "abcdef0", "passed": True}, "member"),
    ("POST", "/tokens", {"name": "test"}, "admin"),
    ("GET", "/tokens", None, "admin"),
    ("DELETE", "/tokens/{token}", None, "admin"),
]


@pytest.mark.parametrize("role", ["viewer", "member", "admin"])
@pytest.mark.parametrize("method,path,body,minimum", WRITE_ROUTES)
async def test_role_matrix(auth_api, store, role, method, path, body, minimum):
    task, _ = await store.create_task(
        tenant_id="a", kind="coding", spec={"repo_url": "/tmp/repo"}, idempotency_key=None
    )
    if path.endswith("review"):
        await awaiting(store, task.id)
    if path.endswith("resume"):
        await store.set_state(task.id, TaskState.PROVISIONING)
        await store.set_state(task.id, TaskState.PAUSED)
    token = (await auth_api.post("/v1/tokens", json={"name": "test"})).json()
    response = await auth_api.request(
        method,
        "/v1" + path.format(id=task.id, token=token["id"]),
        json=body,
        headers={"Authorization": "Bearer " + role},
    )
    allowed = role == "admin" or (role == "member" and minimum == "member")
    assert response.status_code in (200, 201, 204) if allowed else response.status_code == 403
    if not allowed:
        assert response.json()["error"]["code"] == "forbidden"


async def test_restricted_database_token(auth_api, store, api_store):
    token = (
        await auth_api.post(
            "/v1/tokens", json={"name": "CI", "scopes": ["ci:report"], "repo_url": "bound"}
        )
    ).json()
    secret = token["token"]
    assert secret.startswith("sca_") and "token_hash" not in token
    listing = await auth_api.get("/v1/tokens")
    assert secret not in listing.text and "token_hash" not in listing.text
    assert hashlib.sha256(secret.encode()).hexdigest() not in listing.text
    auth = {"Authorization": "Bearer " + secret}
    task, _ = await store.create_task(
        tenant_id="a", kind="coding", spec={"repo_url": "bound"}, idempotency_key=None
    )
    other, _ = await store.create_task(
        tenant_id="b", kind="coding", spec={"repo_url": "bound"}, idempotency_key=None
    )
    wrong, _ = await store.create_task(
        tenant_id="a", kind="coding", spec={"repo_url": "wrong"}, idempotency_key=None
    )
    body = {"commit": "abcdef0", "passed": True}
    for task_id, status in [(task.id, 200), (other.id, 404), (wrong.id, 403)]:
        assert (
            await auth_api.post(f"/v1/tasks/{task_id}/ci-result", json=body, headers=auth)
        ).status_code == status
    for method, path, data, _ in WRITE_ROUTES:
        if path.endswith("ci-result"):
            continue
        response = await auth_api.request(
            method, "/v1" + path.format(id=task.id, token=token["id"]), json=data, headers=auth
        )
        assert response.status_code == 403
    for suffix in [
        "",
        f"/{task.id}",
        f"/{task.id}/events",
        f"/{task.id}/events/stream",
        f"/{task.id}/artifacts",
        f"/{task.id}/artifacts/x",
    ]:
        assert (await auth_api.get("/v1/tasks" + suffix, headers=auth)).status_code == 403
    async with api_store.sessionmaker.begin() as session:
        assert not (await session.execute(text("SELECT * FROM api_tokens"))).all()
    await auth_api.delete("/v1/tokens/" + token["id"])
    assert (await auth_api.get("/v1/tasks", headers=auth)).status_code == 401
    expired = "sca_" + uuid4().hex
    await api_store.for_tenant("a").create_api_token(
        name="expired",
        role="admin",
        scopes=[],
        repo_url=None,
        token_hash=hashlib.sha256(expired.encode()).hexdigest(),
        created_by="test",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert (
        await auth_api.get("/v1/tasks", headers={"Authorization": "Bearer " + expired})
    ).status_code == 401


async def test_audit_actor(auth_api, store):
    response = await auth_api.post("/v1/tasks", json=WRITE_ROUTES[0][2])
    task_id = response.json()["id"]
    await awaiting(store, task_id)
    await auth_api.post(f"/v1/tasks/{task_id}/review", json={"decision": "approve"})
    await auth_api.post(f"/v1/tasks/{task_id}/pause")
    await store.set_state(task_id, TaskState.PAUSED)
    await auth_api.post(f"/v1/tasks/{task_id}/resume")
    await auth_api.post(f"/v1/tasks/{task_id}/cancel")
    events = await store.list_events(task_id)
    for kind in [
        "task.created",
        "review.submitted",
        "task.pause_requested",
        "task.resume_requested",
        "task.cancel_requested",
    ]:
        assert next(e for e in events if e.type == kind).payload["actor"] == "static-token"


@pytest.mark.parametrize("role", ["viewer", "member", "admin"])
@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "/{id}",
        "/{id}/events",
        "/{id}/events/stream",
        "/{id}/artifacts",
        "/{id}/artifacts/changes.patch",
    ],
)
async def test_read_matrix(auth_api, store, role, suffix):
    task, _ = await store.create_task(tenant_id="a", kind="coding", spec={}, idempotency_key=None)
    await store.set_state(task.id, TaskState.CANCELLED)
    response = await auth_api.get(
        "/v1/tasks" + suffix.format(id=task.id), headers={"Authorization": "Bearer " + role}
    )
    assert response.status_code == (404 if suffix.endswith("changes.patch") else 200)
