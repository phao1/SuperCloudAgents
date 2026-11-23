import sys
from types import SimpleNamespace
from typing import Annotated
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.engine import make_url

from sca.api import portal_server
from sca.api.app import create_app
from sca.api.authz import LocalPortalPrincipal, authorize
from sca.config import Settings
from sca.core.models import Principal
from sca.storage.local import LocalObjectStore

ORIGIN = "http://127.0.0.1:8000"
ACCESS = "/v1/portal/access"
ADMIN = {"Authorization": "Bearer fixture-eval"}


@pytest.fixture
def local_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SCA_RUNTIME_MODE", "source")
    monkeypatch.delenv("SCA_PORTAL_LOCAL_CONFIG", raising=False)

    def make(*, port=8000, deleted=False, **overrides) -> FastAPI:
        options = {
            "data_dir": tmp_path, "guest_view": "off", "portal_require_token": False,
            "api_tokens": "fixture-eval:eval:admin,fixture-member:eval:member,fixture-other:other:admin",
            "eval_api_token": "fixture-eval", "platform_admin_token": "fixture-platform",
        }
        settings = Settings(_env_file=None, **(options | overrides))
        monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
        store = SimpleNamespace(
            sessionmaker=SimpleNamespace(kw={"bind": SimpleNamespace(
                url=make_url("postgresql+asyncpg://localhost/sca"),
            )}),
            tenant_deleted=AsyncMock(return_value=deleted),
        )
        app = create_app(store=store, orchestrator=SimpleNamespace(),
                         object_store=LocalObjectStore(tmp_path), local_portal_port=port)

        @app.api_route("/probe", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        async def probe(user: Annotated[Principal, Depends(authorize("read", guest=True))]) -> dict:
            return {"principal": user.model_dump(), "local": isinstance(user, LocalPortalPrincipal)}

        return app

    return make


def client_for(app, *, peer="127.0.0.1", url=ORIGIN):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 43120)), base_url=url)


@pytest.mark.parametrize(("method", "headers", "status"), [
    ("GET", {}, 200),
    ("GET", {"Host": "localhost:8000"}, 200),
    ("GET", {"Host": "[::1]:8000"}, 200),
    ("GET", {"Host": "portal.example:8000"}, 401),
    ("GET", {"Host": "127.0.0.1"}, 401),
    ("GET", {"Host": "127.0.0.1:8001"}, 401),
    ("GET", {"Host": "127.0.0.2:8000"}, 401),
    ("GET", [("Host", "127.0.0.1:8000"), ("Host", "portal.example:8000")], 401),
    ("GET", {"Forwarded": "for=127.0.0.1"}, 401),
    ("GET", {"X-Forwarded-For": "127.0.0.1"}, 401),
    ("GET", {"X-Forwarded-Host": "127.0.0.1:8000"}, 401),
    ("GET", {"X-Real-IP": "127.0.0.1"}, 401),
    ("GET", {"Via": "1.1 proxy"}, 401),
    ("GET", {"Origin": "http://other.example"}, 401),
    ("POST", {"Origin": ORIGIN}, 200),
    ("POST", {"Sec-Fetch-Site": "same-origin"}, 200),
    ("POST", {"Host": "localhost:8000", "Origin": "http://localhost:8000"}, 200),
    ("POST", {"Host": "[::1]:8000", "Origin": "http://[::1]:8000"}, 200),
    ("POST", {"Origin": "http://localhost:8000"}, 401),
    ("POST", {"Origin": "http://127.0.0.1:8001"}, 401),
    ("POST", {"Origin": "https://127.0.0.1:8000"}, 401),
    ("POST", {"Origin": "null"}, 401),
    ("POST", {"Origin": "http://other.example", "Sec-Fetch-Site": "same-origin"}, 401),
    ("POST", {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"}, 401),
    ("POST", {"Sec-Fetch-Site": "same-site"}, 401),
    ("POST", {}, 401),
    ("PUT", {}, 401),
    ("PATCH", {}, 401),
    ("DELETE", {}, 401),
])
async def test_local_access_request_gates(local_app, method, headers, status):
    async with client_for(local_app()) as client:
        response = await client.request(method, "/probe", headers=headers)
        assert response.status_code == status
        if status == 200:
            assert response.json()["local"] is True


@pytest.mark.parametrize(("peer", "status"), [
    ("::1", 200), ("::ffff:127.0.0.1", 200), ("127.2.3.4", 200), ("192.0.2.2", 401),
])
async def test_loopback_socket_peer(local_app, peer, status):
    async with client_for(local_app(), peer=peer) as client:
        response = await client.get("/v1/portal/whoami")
        assert response.status_code == status


@pytest.mark.parametrize(("port", "url"), [(None, ORIGIN), (8000, "http://192.0.2.2:8000")])
async def test_requires_verified_loopback_listener(local_app, port, url):
    async with client_for(local_app(port=port), url=url) as client:
        headers = {"Host": "127.0.0.1:8000"}
        assert (await client.get("/probe", headers=headers)).status_code == 401
        assert (await client.get(ACCESS, headers=headers)).json() == {
            "require_token": True, "local_no_token_active": False,
        }
        assert (await client.put(ACCESS, headers=headers | ADMIN, json={"require_token": False})).status_code == 403


async def test_local_principal_matches_eval_token_even_with_guest_tenant_override(local_app):
    async with client_for(local_app(guest_view="local", guest_tenant="history")) as client:
        local = (await client.get("/probe")).json()
        token = (await client.get("/probe", headers=ADMIN)).json()
        assert local == {**token, "local": True}
        assert local["principal"]["tenant_id"] == "eval"
        assert local["principal"]["role"] == "admin"
        for path in ("/v1/admin/tenants", "/v1/admin/billing"):
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers=ADMIN)).status_code == 403


@pytest.mark.parametrize("value", ["Bearer invalid", "Basic ignored", ""])
async def test_authorization_header_disables_implicit_access(local_app, value):
    async with client_for(local_app(guest_view="local")) as client:
        headers = {"Authorization": value}
        assert (await client.get("/probe", headers=headers)).status_code == 401
        assert (await client.post("/probe", headers=headers | {"Origin": ORIGIN})).status_code == 401


async def test_valid_bearer_uses_its_own_tenant_and_role(local_app):
    async with client_for(local_app()) as client:
        for token, tenant, role in [("fixture-member", "eval", "member"), ("fixture-other", "other", "admin")]:
            response = await client.post("/probe", headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert response.json()["local"] is False
            assert response.json()["principal"]["tenant_id"] == tenant
            assert response.json()["principal"]["role"] == role


@pytest.mark.parametrize("token", [None, "fixture-invalid", "fixture-other", "fixture-member"])
async def test_no_grant_without_a_valid_eval_admin_credential(local_app, token):
    async with client_for(local_app(eval_api_token=token)) as client:
        assert (await client.get("/probe")).status_code == 401
        assert (await client.get(ACCESS)).json()["local_no_token_active"] is False


async def test_no_grant_to_deleted_tenant(local_app):
    async with client_for(local_app(deleted=True, guest_view="local")) as client:
        assert (await client.get("/probe")).status_code == 401
        assert (await client.get("/probe", headers=ADMIN)).status_code == 401


async def test_switch_changes_immediately_and_overrides_launcher_default_after_restart(local_app, tmp_path):
    async with client_for(local_app(guest_view="local", guest_tenant="history")) as client:
        response = await client.get(ACCESS)
        assert response.json() == {"require_token": False, "local_no_token_active": True}
        assert response.headers["Cache-Control"] == "no-store"
        origin = {"Origin": ORIGIN}
        assert (await client.put(ACCESS, json={"require_token": True})).status_code == 401
        response = await client.put(ACCESS, headers=origin, json={"require_token": True})
        assert response.json() == {"require_token": True, "local_no_token_active": False}
        assert (tmp_path / "portal-access.json").read_text() == '{"require_token":true}\n'
        assert (await client.get("/v1/portal/whoami")).status_code == 401
        assert (await client.get("/probe")).json()["principal"]["tenant_id"] == "history"
        assert (await client.post("/probe", headers=origin)).status_code == 401

    async with client_for(local_app()) as client:
        assert (await client.get(ACCESS)).json() == {"require_token": True, "local_no_token_active": False}
        response = await client.put(ACCESS, headers=ADMIN, json={"require_token": False})
        assert response.json() == {"require_token": False, "local_no_token_active": True}

    async with client_for(local_app(portal_require_token=True)) as client:
        assert (await client.get("/probe")).json()["local"] is True


async def test_switch_requires_eval_admin_and_a_boolean(local_app, tmp_path):
    async with client_for(local_app(portal_require_token=True)) as client:
        assert (await client.get("/probe")).status_code == 401
        assert (await client.get("/probe", headers=ADMIN)).status_code == 200
        for token in ("fixture-member", "fixture-other"):
            response = await client.put(ACCESS, headers={"Authorization": f"Bearer {token}"},
                                        json={"require_token": False})
            assert response.status_code == 403
        assert (await client.put(ACCESS, headers=ADMIN, json={"require_token": "false"})).status_code == 422
        assert not (tmp_path / "portal-access.json").exists()


@pytest.mark.parametrize("contents", ["{", "{}", '{"require_token":"false"}'])
async def test_invalid_switch_file_fails_closed(local_app, tmp_path, contents):
    (tmp_path / "portal-access.json").write_text(contents)
    async with client_for(local_app()) as client:
        assert (await client.get(ACCESS)).json() == {"require_token": True, "local_no_token_active": False}
        assert (await client.get("/probe")).status_code == 401
        assert (await client.get("/probe", headers=ADMIN)).status_code == 200


@pytest.mark.parametrize(("host", "proxy_headers", "port"), [
    ("127.0.0.1", False, 8000), ("::1", False, 8000),
    ("0.0.0.0", False, None), ("::", False, None), ("127.0.0.1", True, None),
])
def test_portal_runner_verifies_actual_uvicorn_options(monkeypatch, host, proxy_headers, port):
    monkeypatch.setattr(sys, "argv", ["portal_server", "--host", host, "--port", "8000",
                                    "--proxy-headers" if proxy_headers else "--no-proxy-headers"])
    monkeypatch.setattr(portal_server, "create_app", lambda **options: options)

    def run(app_factory, **options):
        assert options == {"factory": True, "host": host, "port": 8000, "proxy_headers": proxy_headers}
        assert app_factory() == {"local_portal_port": port}

    monkeypatch.setattr(portal_server.uvicorn, "run", run)
    portal_server.main()
