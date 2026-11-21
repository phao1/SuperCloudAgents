from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlalchemy.engine import make_url

from sca.api.app import create_app
from sca.config import Settings
from sca.core.errors import AuthError
from sca.core.model_provider import ModelProviderError
from sca.core.models import Principal
from sca.gateway.health import ProviderHealth
from sca.storage.local import LocalObjectStore


@pytest.fixture
async def portal_client(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path, tenant_max_queued=12,
                        platform_admin_token="fixture-platform", guest_view="off")
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)

    async def authenticate(token):
        roles = {"fixture-reader": "viewer", "fixture-admin": "admin", "fixture-ci": "member"}
        if token not in roles:
            raise AuthError("invalid fixture")
        return Principal(tenant_id="customer", subject="fixture-subject", role=roles[token],
                         scopes=["ci:report"] if token == "fixture-ci" else [])

    queries = []
    plan = {"max_active_tasks": 3, "max_concurrent_tasks": 4, "monthly_cost_limit_usd": 100,
            "max_task_cost_usd": 10}

    def record(query, result):
        queries.append(query.compile().params)
        return result

    session = SimpleNamespace(
        execute=AsyncMock(side_effect=lambda q: record(q, SimpleNamespace(
            mappings=lambda: SimpleNamespace(one_or_none=lambda: plan), one=lambda: (2, 1)))),
        scalar=AsyncMock(side_effect=lambda q: record(q, 1.25)),
    )

    @asynccontextmanager
    async def transaction():
        yield session

    scope = SimpleNamespace(_transaction=transaction)
    database = SimpleNamespace(
        sessionmaker=SimpleNamespace(kw={"bind": SimpleNamespace(
            url=make_url("postgresql+asyncpg://localhost/sca"))}),
        for_tenant=Mock(return_value=scope),
    )
    app = create_app(store=database, authenticator=SimpleNamespace(authenticate=authenticate),
                     object_store=LocalObjectStore(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, database, queries


def bearer(token):
    return {"Authorization": "Bearer " + token}


async def test_portal_identifies_tenant_and_platform_tokens(portal_client):
    client, database, _ = portal_client
    path = "/v1/portal/whoami"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=bearer("fixture-ci"))).status_code == 403
    assert (await client.get(path, headers=bearer("fixture-reader"))).json() == {
        "tenant_id": "customer", "role": "viewer", "subject": "fixture-subject",
        "platform_admin": False,
    }
    assert (await client.get(path, headers=bearer("fixture-platform"))).json() == {"platform_admin": True}
    database.for_tenant.assert_not_called()


async def test_portal_quota_only_uses_the_authenticated_tenant(portal_client):
    client, database, queries = portal_client
    path = "/v1/portal/tenant-quota?tenant_id=someone-else"
    assert (await client.get(path, headers=bearer("fixture-reader"))).status_code == 403
    assert (await client.get(path, headers=bearer("fixture-platform"))).status_code == 401
    response = await client.get(path, headers=bearer("fixture-admin"))
    assert response.status_code == 200
    assert response.json() == {
        "monthly_cost_usd": 1.25, "monthly_cost_limit_usd": 100, "max_task_cost_usd": 10,
        "slots_used": 2, "slots_limit": 3, "queued": 1, "queued_limit": 12,
    }
    database.for_tenant.assert_called_once_with("customer")
    assert len(queries) == 3
    assert all("customer" in values.values() and "someone-else" not in values.values()
               for values in queries)


async def test_portal_serves_only_registered_assets(portal_client):
    client, _, _ = portal_client
    page = await client.get("/portal")
    assert page.status_code == 200
    for file in ("portal-ui.js", "portal-markdown.js", "portal-tasks.js", "portal-evolution.js",
                 "portal-settings.js", "portal-projects.js", "portal-operations.css"):
        path = f"/portal/assets/{file}"
        assert path in page.text
        response = await client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(
            "text/javascript" if file.endswith(".js") else "text/css"
        )
    assert (await client.get("/portal/assets/portal.html")).status_code == 404


async def test_portal_config_reports_shared_provider_health(portal_client, monkeypatch, tmp_path):
    client, _, _ = portal_client
    settings = Settings(_env_file=None, data_dir=tmp_path, model_gateway_url='http://gateway')
    monkeypatch.setattr('sca.api.portal.get_settings', lambda: settings)
    monkeypatch.setattr('sca.api.portal.gateway_models', AsyncMock(return_value=(True, ['model'])))
    health = ProviderHealth(tmp_path)
    health.record(ModelProviderError('auth', 401))
    health.record(ModelProviderError('unavailable', 503))
    path = '/v1/portal/config'
    assert (await client.get(path)).status_code == 401
    headers = bearer('fixture-reader')
    result = (await client.get(path, headers=headers)).json()
    assert result['gateway_reachable'] is True
    assert result['gateway_provider_account_error'] == health.read()['account_error']
    assert result['gateway_provider_error']['kind'] == 'unavailable'
    health.record(None)
    result = (await client.get(path, headers=headers)).json()
    assert result['gateway_provider_error'] is result['gateway_provider_account_error'] is None
