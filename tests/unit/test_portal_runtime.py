import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest
from sqlalchemy.engine import make_url

from sca import runtime_switch
from sca.api import portal_runtime
from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings

ENDPOINT = "/v1/portal/runtime"
ORIGIN = "http://127.0.0.1:8000"
ADMIN = {"Authorization": "Bearer fixture-eval"}


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "checkout with spaces"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/portal_switch.py").touch()
    monkeypatch.setenv("SCA_PORTAL_LOCAL_CONFIG", str(root / "fixture.env"))
    monkeypatch.setenv("SCA_RUNTIME_MODE", "source")
    monkeypatch.setattr(portal_runtime.platform, "system", lambda: "Linux")
    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(portal_runtime, "docker_unavailable", probe)
    start = Mock()
    monkeypatch.setattr(runtime_switch, "launch_switch", start)
    tokens = {"fixture-eval": "eval:admin", "fixture-member": "eval:member", "fixture-other": "other:admin"}
    settings = Settings(_env_file=None, data_dir=tmp_path / "configured-data", guest_view="off",
                        portal_require_token=False, eval_api_token="fixture-eval")
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)

    def client(*, port=8000, peer="127.0.0.1"):
        store = SimpleNamespace(sessionmaker=SimpleNamespace(kw={"bind": SimpleNamespace(
            url=make_url("postgresql+asyncpg://localhost/sca"),
        )}))
        app = create_app(store=store, orchestrator=SimpleNamespace(),
                         authenticator=StaticTokenAuthenticator(tokens), local_portal_port=port)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 2000)), base_url=ORIGIN)

    return SimpleNamespace(root=root, data=root / ".data", client=client, start=start, probe=probe)


@pytest.mark.parametrize(("peer", "port", "headers", "status"), [
    ("192.0.2.2", 8000, ADMIN, 403),
    ("127.0.0.1", None, ADMIN, 403),
    ("127.0.0.1", 8000, ADMIN | {"Host": "remote.example:8000"}, 403),
    ("127.0.0.1", 8000, ADMIN | {"X-Forwarded-For": "192.0.2.2"}, 403),
    ("127.0.0.1", 8000, {"Authorization": "Bearer fixture-other"}, 403),
    ("127.0.0.1", 8000, {"Authorization": "Bearer fixture-member"}, 403),
    ("127.0.0.1", 8000, {"Origin": "http://remote.example"}, 401),
])
async def test_runtime_routes_require_a_local_eval_admin(installation, peer, port, headers, status):
    async with installation.client(peer=peer, port=port) as client:
        assert (await client.get(ENDPOINT, headers=headers)).status_code == status
        response = await client.post(ENDPOINT + "/switch", headers=headers, json={"mode": "docker"})
        assert response.status_code == status
    installation.start.assert_not_called()
    installation.probe.assert_not_called()


async def test_runtime_requires_launcher_config(installation, monkeypatch):
    monkeypatch.delenv("SCA_PORTAL_LOCAL_CONFIG")
    async with installation.client() as client:
        assert (await client.get(ENDPOINT, headers=ADMIN)).status_code == 403


async def test_windows_lists_source_only_without_probing_docker(installation, monkeypatch):
    monkeypatch.setattr(portal_runtime.platform, "system", lambda: "Windows")
    async with installation.client() as client:
        payload = (await client.get(ENDPOINT)).json()
        assert payload["platform"] == "Windows" and payload["current_mode"] == "source"
        assert payload["modes"] == [
            {"mode": "source", "available": True, "reason": None},
            {"mode": "docker", "available": False, "reason": "Windows 版仅支持源码模式"},
        ]
        response = await client.post(ENDPOINT + "/switch", headers=ADMIN, json={"mode": "docker"})
        assert response.status_code == 409 and "Windows 版仅支持源码模式" in response.text
        response = await client.post(ENDPOINT + "/switch", headers=ADMIN, json={"mode": "source"})
        assert response.status_code == 409
    installation.probe.assert_not_called()
    installation.start.assert_not_called()
    assert not installation.data.exists()


async def test_switch_returns_202_and_persists_preference_and_status(installation, monkeypatch):
    async with installation.client() as client:
        response = await client.get(ENDPOINT)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.json() == {
            "current_mode": "source", "platform": "Linux", "preferred_mode": None, "switch": None,
            "modes": [{"mode": mode, "available": True, "reason": None} for mode in ("source", "docker")],
        }
        body = {"mode": "docker", "carry_history": True}
        assert (await client.post(ENDPOINT + "/switch", json=body)).status_code == 401
        response = await client.post(ENDPOINT + "/switch", headers={"Origin": ORIGIN}, json=body)
        assert response.status_code == 202
        job_id = UUID(response.json()["id"])
        installation.start.assert_called_once_with(installation.root, job_id)
        assert json.loads((installation.data / "runtime-mode.json").read_text()) == {"mode": "docker"}
        record = runtime_switch.read_switch(installation.data)
        assert record is not None and record.id == job_id and record.carry_history
        assert record.source == "source" and record.target == "docker"
        assert record.started_at <= record.updated_at
        response = await client.post(ENDPOINT + "/switch", headers=ADMIN, json=body)
        assert response.status_code == 409

    record.report(installation.data, "done", status="succeeded")
    monkeypatch.setenv("SCA_RUNTIME_MODE", "docker")
    monkeypatch.setenv("SCA_DOCKER_BIN", "fixture-docker")
    async with installation.client() as client:
        payload = (await client.get(ENDPOINT, headers=ADMIN)).json()
        assert payload["current_mode"] == payload["preferred_mode"] == "docker"
        assert payload["switch"] == {
            "id": str(job_id), "step": "done", "status": "succeeded", "error": None,
            "log_tail": record.log_tail,
        }


async def test_failed_spawn_restores_preference_and_reports_safe_error(installation):
    runtime_switch.save_preference(installation.data, "source")
    installation.start.side_effect = OSError("fixture-only sensitive error")
    async with installation.client() as client:
        response = await client.post(ENDPOINT + "/switch", headers=ADMIN, json={"mode": "docker"})
        assert response.status_code == 503 and "fixture-only" not in response.text
        result = (await client.get(ENDPOINT, headers=ADMIN)).json()
        assert result["preferred_mode"] == "source"
        assert result["switch"]["status"] == "failed"
        assert "fixture-only" not in str(result)


@pytest.mark.parametrize(("exit_code", "reason"), [
    (0, None), (2, "CLI"), (3, "无法连接 Docker"), (4, "Compose"), (5, "GNU timeout"),
])
async def test_docker_probe_uses_launcher_without_echoing_its_output(tmp_path, monkeypatch, exit_code, reason):
    process = SimpleNamespace(returncode=exit_code, wait=AsyncMock(return_value=exit_code))
    command = AsyncMock(return_value=process)
    monkeypatch.setattr(portal_runtime.asyncio, "create_subprocess_exec", command)
    reported = await portal_runtime.docker_unavailable(tmp_path)
    assert reported is None if reason is None else reason in reported
    command.assert_awaited_once_with(
        "bash", str(tmp_path / "scripts/portal_demo.sh"), "probe-docker", cwd=tmp_path,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
