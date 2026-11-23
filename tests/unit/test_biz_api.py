import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from sca.api.app import create_app
from sca.api.auth import OidcAuthenticator
from sca.billing.quota import PlanQuotaExceededError
from sca.cli import app as cli
from sca.config import Settings
from sca.core.errors import AuthError
from sca.db.repo import Store
from sca.storage.local import LocalObjectStore
from sca.tenancy.models import TenantSuspendedError


@pytest.fixture
async def api(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None, data_dir=tmp_path,
        api_tokens="tenant-fixture:acme:admin,viewer-fixture:acme:viewer",
        platform_admin_token="platform-fixture", billing_markup="1.5",
        billing_base_fees_usd={"team": "20"},
    )
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    monkeypatch.setattr("sca.api.usage.get_settings", lambda: settings)
    today = datetime.now(UTC).date()
    tenant = {"id": "acme", "display_name": "Acme", "plan": "team", "status": "active"}
    usage = {"tenant_id": "acme", "day": today, "tasks": 1, "turns": 2, "tokens_in": 3,
             "tokens_out": 4, "cost_usd": Decimal(1), "sandbox_seconds": 9, "by_model": {}}
    scoped = SimpleNamespace(get_tenant=AsyncMock(return_value=tenant),
                             create_task=AsyncMock(),
                             usage_rows=AsyncMock(return_value=[usage]))
    db = SimpleNamespace(
        sessionmaker=SimpleNamespace(kw={"bind": SimpleNamespace(
            url=make_url("postgresql+asyncpg://localhost/sca"))}),
        for_tenant=Mock(return_value=scoped), tenant_deleted=AsyncMock(return_value=False),
        check_tenant_submission=AsyncMock(),
    )
    platform = SimpleNamespace(list_tenants=AsyncMock(return_value=[tenant]),
                               usage_rows=AsyncMock(return_value=[usage]))
    app = create_app(store=db, platform_store=platform, object_store=LocalObjectStore(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test") as client:
        yield client, db, scoped


async def test_tenant_admin_cannot_access_platform_and_export_is_scoped(api):
    client, db, scoped = api
    tenant = {"Authorization": "Bearer tenant-fixture"}
    platform = {"Authorization": "Bearer platform-fixture"}
    assert (await client.get("/v1/admin/tenants")).status_code == 401
    assert (await client.get("/v1/admin/tenants", headers=tenant)).status_code == 403
    assert (await client.get("/v1/admin/tenants", headers=platform)).json()[0]["id"] == "acme"
    assert (await client.get("/v1/admin/usage", headers=tenant)).status_code == 403
    assert (await client.get("/v1/admin/usage", headers=platform)).json()[0]["tenant_id"] == "acme"
    assert (await client.get("/v1/tenant", headers=tenant)).json()["plan"] == "team"
    assert (await client.get("/v1/usage", headers=tenant)).json()[0]["tasks"] == 1
    export = await client.get("/v1/usage/export.csv", headers=tenant)
    assert export.status_code == 200
    assert export.text.splitlines()[1].endswith(",1,1.5,20,21.50")
    assert db.for_tenant.call_args.args == ("acme",)
    assert scoped.usage_rows.await_count == 2
    invalid = await client.get("/v1/usage?from=2026-02-04&to=2026-02-01", headers=tenant)
    assert invalid.status_code == 422


async def test_today_task_cost_uses_the_admins_tenant_and_utc_day(api, monkeypatch):
    client, db, scoped = api
    midnight = datetime(2026, 11, 23, tzinfo=UTC)
    clock = SimpleNamespace(now=Mock(return_value=midnight + timedelta(hours=12)))
    monkeypatch.setattr("sca.api.usage.datetime", clock)
    session = SimpleNamespace(scalar=AsyncMock(return_value=Decimal(4200)))

    @asynccontextmanager
    async def transaction():
        yield session

    store = Store(db.sessionmaker).for_tenant("acme")
    monkeypatch.setattr(store, "_transaction", transaction)
    scoped.task_cost_for_day = store.task_cost_for_day
    path = "/v1/usage/today?tenant_id=other"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers={"Authorization": "Bearer viewer-fixture"})).status_code == 403
    db.for_tenant.assert_not_called()

    response = await client.get(path, headers={"Authorization": "Bearer tenant-fixture"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"day": "2026-11-23", "cost_usd": 0.0042}
    db.for_tenant.assert_called_once_with("acme")
    clock.now.assert_called_once_with(UTC)
    session.scalar.assert_awaited_once()
    query = session.scalar.await_args.args[0].compile(dialect=postgresql.dialect())
    for value in ("acme", midnight, midnight + timedelta(days=1), {"history_imported": True}):
        assert value in query.params.values()
    assert "tasks.tenant_id =" in str(query)
    assert "tasks.created_at >=" in str(query) and "tasks.created_at <" in str(query)
    assert "tasks.state" not in str(query) and "usage_daily" not in str(query)
    scoped.usage_rows.assert_not_awaited()


async def test_submit_errors_keep_distinct_http_statuses(api):
    client, db, scoped = api
    headers = {"Authorization": "Bearer tenant-fixture"}
    body = {"kind": "coding", "spec": {"repo_url": "/repo", "instruction": "fix"}}
    db.check_tenant_submission.side_effect = TenantSuspendedError()
    reply = await client.post("/v1/tasks", headers=headers, json=body)
    assert reply.status_code == 403
    assert "租户已暂停" in reply.json()["error"]["message"]
    scoped.create_task.assert_not_awaited()

    db.check_tenant_submission.side_effect = None
    scoped.create_task.side_effect = PlanQuotaExceededError("租户本月额度已用完")
    reply = await client.post("/v1/tasks", headers=headers, json=body)
    assert reply.status_code == 402
    assert reply.json()["error"]["code"] == "quota_exceeded"


async def test_oidc_requires_verified_email_to_activate_and_uses_membership_role(monkeypatch):
    async def candidate_tenants(subject, email):
        return ["acme"] if email == "owner@example.com" else []

    scoped = SimpleNamespace(resolve_oidc_membership=AsyncMock(return_value=(True, "owner")))
    db = SimpleNamespace(oidc_member_tenants=AsyncMock(side_effect=candidate_tenants),
                         for_tenant=Mock(return_value=scoped))
    auth = OidcAuthenticator("https://issuer.example", "sca", "https://issuer.example/keys",
                             store=db)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.PyJWK.from_json(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    monkeypatch.setattr(auth.jwks, "fetch_data", lambda: {"keys": [{**jwk._jwk_data, "kid": "k"}]})

    def token(*, verified, **claims):
        return jwt.encode({"iss": auth.issuer, "aud": "sca", "exp": time.time() + 60, "sub": "owner",
                           "email": "Owner@Example.com", "email_verified": verified, **claims},
                          key, algorithm="RS256", headers={"kid": "k"})

    with pytest.raises(AuthError, match="Missing tenant claim or membership"):
        await auth.authenticate(token(verified=False))
    scoped.resolve_oidc_membership.assert_not_awaited()
    owner = await auth.authenticate(token(verified=True, roles=["viewer"]))
    assert (owner.tenant_id, owner.subject, owner.role) == ("acme", "owner", "admin")
    scoped.resolve_oidc_membership.assert_awaited_once_with("owner", "owner@example.com")
    scoped.resolve_oidc_membership.return_value = (True, None)
    with pytest.raises(AuthError, match="not a member"):
        await auth.authenticate(token(verified=True, tenant="other", roles=["admin"]))
    assert db.for_tenant.call_args.args == ("other",)


def test_usage_backfill_cli_runs_operator_store(monkeypatch):
    engine = SimpleNamespace(dispose=AsyncMock())
    store = SimpleNamespace(usage_backfill=AsyncMock(return_value=7),
                            sessionmaker=SimpleNamespace(kw={"bind": engine}))
    monkeypatch.setattr("sca.db.engine.get_store", lambda: store)
    result = CliRunner().invoke(cli, ["admin", "usage-backfill"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"tasks_processed": 7}
    store.usage_backfill.assert_awaited_once()
    engine.dispose.assert_awaited_once()
