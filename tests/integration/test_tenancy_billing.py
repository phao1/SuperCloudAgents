import asyncio
import csv
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_api import FakeOrchestrator

from sca.api.app import create_app
from sca.archive import archive_task
from sca.config import Settings
from sca.core.models import TaskState
from sca.kinds.coding.subtasks import ChildStore
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration
PLATFORM = {"Authorization": "Bearer platform-fixture"}
TENANT = {"Authorization": "Bearer tenant-fixture"}
OTHER = {"Authorization": "Bearer other-fixture"}
TASK = {"kind": "coding", "spec": {"repo_url": "/tmp/repo", "instruction": "fix"}}


@pytest.fixture
async def commercial_api(store, api_store, tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None, data_dir=tmp_path, platform_admin_token="platform-fixture",
        api_tokens="tenant-fixture:acme:admin,other-fixture:other:admin",
        oidc_issuer="https://issuer.example", oidc_audience="sca",
        billing_markup=Decimal(2), billing_base_fees_usd={"free": Decimal(3)},
    )
    monkeypatch.setattr("sca.config.get_settings", lambda: settings)
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    monkeypatch.setattr("sca.api.usage.get_settings", lambda: settings)
    objects = LocalObjectStore(settings.objects_dir())
    store.objects = objects
    app = create_app(store=api_store, orchestrator=FakeOrchestrator(store), object_store=objects,
                     platform_store=store)
    oidc = app.state.authenticator.authenticators[-1]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.PyJWK.from_json(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    monkeypatch.setattr(oidc.jwks, "fetch_data", lambda: {"keys": [{**jwk._jwk_data, "kid": "k"}]})

    def oidc_headers(subject, **claims):
        token = jwt.encode({"iss": settings.oidc_issuer, "aud": "sca", "exp": time.time() + 60,
                            "sub": subject, **claims}, key, algorithm="RS256", headers={"kid": "k"})
        return {"Authorization": "Bearer " + token}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        yield client, oidc_headers, settings


async def create_tenant(client, tenant_id="acme", **fields):
    return await client.post("/v1/admin/tenants", headers=PLATFORM,
                             json={"id": tenant_id, "display_name": tenant_id, **fields})


async def new_task(store, tenant_id):
    task, _ = await store.create_task(tenant_id=tenant_id, kind="coding",
                                      spec={"model": "model-a"}, idempotency_key=None)
    return task


async def test_tenant_lifecycle_and_oidc_invitations(commercial_api):
    client, oidc, _ = commercial_api
    assert (await client.get("/v1/admin/tenants", headers=TENANT)).status_code == 403
    assert (await client.get("/v1/admin/usage", headers=TENANT)).status_code == 403
    owner_email = "owner@example.com"
    created = await create_tenant(client, owner_email=owner_email)
    assert created.status_code == 201
    assert (created.json()["plan"], created.json()["status"]) == ("free", "active")
    pending = (await client.get("/v1/tenant/members", headers=TENANT)).json()
    assert [(row["role"], row["status"]) for row in pending] == [("owner", "pending")]

    unverifiable = oidc("different", email=owner_email, email_verified=False)
    assert (await client.get("/v1/tenant", headers=unverifiable)).status_code == 401
    outsider = oidc("different", tenant="acme", email="outsider@example.com",
                    email_verified=True, roles=["admin"])
    assert (await client.get("/v1/tenant", headers=outsider)).status_code == 401
    owner = oidc("owner", email=owner_email.upper(), email_verified=True)
    updated = await client.patch("/v1/tenant", headers=owner, json={"display_name": "Acme"})
    assert updated.json()["display_name"] == "Acme"

    member = await client.post("/v1/tenant/members", headers=owner, json={
        "email": "reader@example.com", "role": "viewer",
    })
    assert member.status_code == 201 and member.json()["status"] == "pending"
    reader = oidc("reader", tenant="acme", email="reader@example.com", email_verified=True,
                  roles=["admin"])
    assert (await client.get("/v1/tasks", headers=reader)).status_code == 200
    assert (await client.post("/v1/tasks", headers=reader, json=TASK)).status_code == 403
    active = (await client.get("/v1/tenant/members", headers=owner)).json()
    assert {(row["role"], row["status"]) for row in active} == {
        ("owner", "active"), ("viewer", "active"),
    }
    impostor = oidc("different", tenant="acme", email=owner_email, email_verified=True)
    assert (await client.get("/v1/tasks", headers=impostor)).status_code == 401

    assert (await client.patch("/v1/admin/tenants/acme", headers=PLATFORM,
                               json={"plan": "team"})).json()["plan"] == "team"
    assert (await client.get("/v1/admin/tenants/acme", headers=PLATFORM)).json()["display_name"] == "Acme"
    assert (await client.delete("/v1/admin/tenants/acme", headers=PLATFORM)).status_code == 204
    assert (await client.get("/v1/admin/tenants", headers=PLATFORM)).json() == []
    assert (await client.get("/v1/tasks", headers=owner)).status_code == 401
    assert (await client.get("/v1/tasks", headers=TENANT)).status_code == 401
    assert (await create_tenant(client)).status_code == 409


async def test_tenant_tokens_are_scoped_and_revocable(commercial_api):
    client, _, _ = commercial_api
    assert (await create_tenant(client)).status_code == 201
    issued = await client.post("/v1/tenant/tokens", headers=TENANT,
                               json={"name": "Reader", "role": "viewer"})
    assert issued.status_code == 201
    assert issued.headers["cache-control"] == "no-store"
    row = issued.json()
    raw = row.pop("token")
    reader = {"Authorization": "Bearer " + raw}
    tokens = (await client.get("/v1/tenant/tokens", headers=TENANT)).json()
    assert tokens[0]["id"] == row["id"] and "token" not in tokens[0]
    assert "token_hash" not in row and "token_hash" not in tokens[0]
    assert (await client.get("/v1/tenant/tokens", headers=OTHER)).json() == []
    assert (await client.get("/v1/tasks", headers=reader)).status_code == 200
    assert (await client.post("/v1/tasks", headers=reader, json=TASK)).status_code == 403
    path = f"/v1/tenant/tokens/{row['id']}"
    assert (await client.delete(path, headers=OTHER)).status_code == 404
    assert (await client.delete(path, headers=TENANT)).status_code == 204
    assert (await client.get("/v1/tasks", headers=reader)).status_code == 401


async def test_billing_tables_enforce_rls(store, api_store):
    for tenant in ("acme", "other"):
        await store.create_tenant(tenant, tenant, "free", f"{tenant}@example.com")
        task = await new_task(store, tenant)
        await store.set_state(task.id, TaskState.CANCELLED)
    for tenant in (None, "acme"):
        scoped = api_store if tenant is None else api_store.for_tenant(tenant)
        async with scoped._transaction() as session:
            for table, column in (("tenants", "id"), ("tenant_members", "tenant_id"),
                                  ("usage_daily", "tenant_id")):
                result = await session.scalars(text(f"SELECT {column} FROM {table}"))
                assert list(result) == ([] if tenant is None else ["acme"])
            assert set(await session.scalars(text("SELECT name FROM plans"))) == {
                "free", "team", "enterprise",
            }
    for sql in (
        "INSERT INTO tenant_members (tenant_id,email,role) VALUES ('other','x@example.com','admin')",
        "UPDATE tenants SET status='suspended' WHERE id='acme'",
        "UPDATE usage_daily SET cost_usd=0 WHERE tenant_id='acme'",
    ):
        with pytest.raises(DBAPIError):
            async with api_store.for_tenant("acme")._transaction() as session:
                await session.execute(text(sql))


async def test_plan_limits_and_suspension(commercial_api, store):
    client, _, _ = commercial_api
    assert (await create_tenant(client)).status_code == 201
    over = await client.post("/v1/tasks", headers=TENANT, json={
        **TASK, "spec": {**TASK["spec"], "max_cost_usd": 5},
    })
    assert over.status_code == 402 and over.json()["error"]["code"] == "quota_exceeded"
    key = {**TENANT, "Idempotency-Key": "first"}
    first = (await client.post("/v1/tasks", headers=key, json=TASK)).json()
    second = (await client.post("/v1/tasks", headers=TENANT, json=TASK)).json()
    assert first["spec"]["max_cost_usd"] == 1
    assert (await store.acquire_slot(first["id"], 4))["granted"]
    assert (await store.acquire_slot(second["id"], 4))["reason"] == "tenant_limit"
    await store.add_usage(first["id"], {"cost_micro_usd": 9_750_000})
    await store.set_state(first["id"], TaskState.CANCELLED)
    assert (await store.acquire_slot(second["id"], 4))["granted"]
    remaining = (await client.post("/v1/tasks", headers=TENANT, json=TASK)).json()
    assert remaining["spec"]["max_cost_usd"] == 0.25
    await store.add_usage(remaining["id"], {"cost_micro_usd": 250_000})
    await store.set_state(remaining["id"], TaskState.CANCELLED)
    blocked = await client.post("/v1/tasks", headers=TENANT, json=TASK)
    assert blocked.status_code == 402 and blocked.json()["error"]["code"] == "quota_exceeded"
    assert (await client.post("/v1/tasks", headers=key, json=TASK)).json()["id"] == first["id"]

    suspend = await client.post("/v1/admin/tenants/acme/suspend", headers=PLATFORM)
    assert suspend.json()["status"] == "suspended"
    blocked = await client.post("/v1/tasks", headers=key, json=TASK)
    assert blocked.status_code == 403
    assert "租户已暂停" in blocked.json()["error"]["message"]
    assert (await client.post("/v1/admin/tenants/acme/resume",
                              headers=PLATFORM)).json()["status"] == "active"
    assert (await client.post("/v1/tasks", headers=key, json=TASK)).status_code == 201


async def test_terminal_metering_and_exports(commercial_api, store):
    client, _, settings = commercial_api
    assert (await create_tenant(client)).status_code == 201
    parent = await new_task(store, "acme")
    other = await new_task(store, "other")
    for source, cost in ((store, 100_000), (ChildStore(store, "sub"),
                         150_000), (ChildStore(store, "best-of-runner-up"), 250_000)):
        await source.append_event(parent.id, "agent.turn_started", {"turn": 1, "model": "model-a"})
        await source.append_event(parent.id, "agent.usage", {"turn": 1, "input_tokens": 10,
                                                             "output_tokens": 5})
        await source.add_usage(parent.id, {"cost_micro_usd": cost})
    task_dir = settings.task_dir(str(parent.id))
    cache = task_dir / "agent_home" / ".cache" / "opencode" / "bin"
    cache.mkdir(parents=True)
    (cache / "binary").write_bytes(b"test")
    empty = await new_task(store, "acme")
    await asyncio.gather(*(store.set_state(task.id, TaskState.CANCELLED)
                           for task in (empty, parent, other)))
    assert cache.exists()
    payload = {"sandbox_alive_s": 12}
    await store.append_event(parent.id, "sandbox.activity", payload, dedup_key="sandbox.activity")
    await store.append_event(parent.id, "sandbox.activity", payload, dedup_key="sandbox.activity")
    await store.append_event(parent.id, "sandbox.destroyed", {}, dedup_key="sandbox.destroyed")
    assert not cache.exists() and task_dir.exists()
    assert any(event.type == "maintenance.disk_reclaimed"
               for event in await store.list_events(parent.id))

    assert (await store.get_task(parent.id)).usage["cost_micro_usd"] == 500_000
    [day] = (await client.get("/v1/usage", headers=TENANT)).json()
    assert day["tasks"] == 2 and day["turns"] == 3
    assert day["tokens_in"] == 30 and day["tokens_out"] == 15
    assert day["cost_usd"] == 0.5 and day["sandbox_seconds"] == 12
    [model] = (await client.get("/v1/usage?group_by=model", headers=TENANT)).json()
    assert model["model"] == "model-a" and model["cost_usd"] == 0.5
    assert {row["tenant_id"] for row in
            (await client.get("/v1/admin/usage", headers=PLATFORM)).json()} == {"acme", "other"}
    csv_response = await client.get("/v1/usage/export.csv", headers=TENANT)
    [row] = csv.DictReader(StringIO(csv_response.text))
    assert row["base_fee_usd"] == "3" and row["total_usd"] == "4.00"


async def test_today_task_cost_includes_running_and_review_without_double_counting(commercial_api, store):
    client, _, _ = commercial_api
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    review, running, finished, yesterday, tomorrow, imported = [
        await new_task(store, "acme") for _ in range(6)
    ]
    other = await new_task(store, "other")
    for task, cost in ((review, 3000), (running, 1000), (finished, 200), (yesterday, 5000),
                       (tomorrow, 6000), (imported, 7000), (other, 8000)):
        await store.add_usage(task.id, {"cost_micro_usd": cost})
    for task in (review, running):
        await store.set_state(task.id, TaskState.PROVISIONING)
        await store.set_state(task.id, TaskState.RUNNING)
    await store.set_state(review.id, TaskState.VERIFYING)
    await store.set_state(review.id, TaskState.AWAITING_REVIEW)
    await store.set_state(finished.id, TaskState.CANCELLED)
    await archive_task(store, store.objects, finished.id)
    await store.update_result(imported.id, {"history_imported": True})
    await store.set_state(imported.id, TaskState.CANCELLED)
    async with store._transaction() as session:
        for task, created_at in ((review, midnight), (yesterday, midnight - timedelta(microseconds=1)),
                                 (tomorrow, midnight + timedelta(days=1))):
            await session.execute(text("UPDATE tasks SET created_at = :created_at WHERE id = :id"),
                                  {"id": task.id, "created_at": created_at})

    path = "/v1/usage/today"
    assert (await client.get(path + "?tenant_id=other", headers=TENANT)).json() == {
        "day": now.date().isoformat(), "cost_usd": 0.0042,
    }
    assert (await client.get(path, headers=OTHER)).json()["cost_usd"] == 0.008
    [settled] = (await client.get("/v1/usage", headers=TENANT)).json()
    assert settled["cost_usd"] == 0.0002
    await store.set_state(review.id, TaskState.SUCCEEDED)
    assert (await client.get(path, headers=TENANT)).json()["cost_usd"] == 0.0042


async def test_backfill_reads_archived_histories_and_is_idempotent(store, tmp_path):
    objects = LocalObjectStore(tmp_path)
    store.objects = objects
    task = await new_task(store, "legacy")
    await store.add_usage(task.id, {"input_tokens": 4, "cost_micro_usd": 125_000})
    await store.set_state(task.id, TaskState.FAILED)
    completed = datetime.now(UTC) - timedelta(days=40)
    async with store._transaction() as session:
        await session.execute(text("""UPDATE events SET created_at = :completed
            WHERE task_id = :id AND type = 'task.state_changed'"""),
                              {"id": task.id, "completed": completed})
        await session.execute(text("UPDATE tasks SET finished_at=NULL, billing_usage=NULL"))
        await session.execute(text("DELETE FROM usage_daily"))
    await archive_task(store, objects, task.id)
    assert await store.usage_backfill() == 1
    assert await store.usage_backfill() == 1
    [day] = await store.usage_rows(completed.date(), completed.date())
    assert (day["tasks"], day["tokens_in"], day["cost_usd"]) == (1, 4, Decimal("0.125"))


async def test_operator_bootstraps_a_tenant_admin_token_without_oidc(commercial_api):
    client, _, _ = commercial_api
    assert (await create_tenant(client, "bootstrap")).status_code == 201
    path = "/v1/admin/tenants/bootstrap/tokens"
    assert (await client.post(path, headers=TENANT, json={"name": "Owner"})).status_code == 403
    assert (await client.post("/v1/admin/tenants/missing/tokens", headers=PLATFORM,
                              json={"name": "Owner"})).status_code == 404
    issued = await client.post(path, headers=PLATFORM, json={"name": "Owner"})
    assert issued.status_code == 201 and issued.headers["cache-control"] == "no-store"
    owner = {"Authorization": "Bearer " + issued.json()["token"]}
    assert issued.json()["role"] == "admin"
    assert (await client.get("/v1/tenant", headers=owner)).json()["id"] == "bootstrap"
    member = await client.post("/v1/tenant/tokens", headers=owner, json={"name": "CI", "role": "member"})
    assert member.status_code == 201
    assert (await client.get("/v1/tenant/tokens", headers=TENANT)).json() == []


async def test_plan_limits_are_json_numbers(commercial_api):
    client, _, _ = commercial_api
    plans = {p["name"]: p for p in (await client.get("/v1/admin/plans", headers=PLATFORM)).json()}
    assert plans["free"]["monthly_cost_limit_usd"] == 10 and plans["enterprise"]["max_task_cost_usd"] is None


async def test_tenant_admin_changes_roles_and_removes_members_but_not_owners(commercial_api):
    client, _, _ = commercial_api
    assert (await create_tenant(client, owner_email="boss@acme.example")).status_code == 201
    invite = await client.post("/v1/tenant/members", headers=TENANT,
                               json={"email": "dev@acme.example", "role": "viewer"})
    assert invite.status_code == 201
    path = "/v1/tenant/members/dev@acme.example"
    assert (await client.patch(path, headers=OTHER, json={"role": "admin"})).status_code == 404
    changed = await client.patch(path, headers=TENANT, json={"role": "member"})
    assert changed.status_code == 200 and changed.json()["role"] == "member"
    assert (await client.patch(path, headers=TENANT, json={"role": "owner"})).status_code == 422
    owner = "/v1/tenant/members/boss@acme.example"
    assert (await client.patch(owner, headers=TENANT, json={"role": "viewer"})).status_code == 409
    assert (await client.delete(owner, headers=TENANT)).status_code == 409
    assert (await client.delete(path, headers=OTHER)).status_code == 404
    assert (await client.delete(path, headers=TENANT)).status_code == 204
    emails = [m["email"] for m in (await client.get("/v1/tenant/members", headers=TENANT)).json()]
    assert emails == ["boss@acme.example"]
