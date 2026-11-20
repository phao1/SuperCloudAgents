import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import NoResultFound

from sca.agents.opencode_build import reserve_build
from sca.api import agents
from sca.api import app as api_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.evolve.solidify import ChampionSolidifyWorkflow


@pytest.fixture
async def api(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    monkeypatch.setattr(agents, "get_settings", lambda: settings)
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    champion = {"id": uuid4(), "candidate_id": uuid4(), "agent_version": "2.3.0-sca.1"}
    tenant = SimpleNamespace(
        get_champion=AsyncMock(return_value=champion), set_default_opencode_version=AsyncMock(),
    )
    bind = SimpleNamespace(url=make_url(settings.database_url))
    store = SimpleNamespace(
        sessionmaker=SimpleNamespace(kw={"bind": bind}), for_tenant=lambda _: tenant,
    )
    temporal = SimpleNamespace(start_workflow=AsyncMock())
    app = api_app.create_app(
        store=store, orchestrator=SimpleNamespace(client=temporal),
        authenticator=StaticTokenAuthenticator({
            "operator": "eval:admin", "member": "eval:member", "tenant-admin": "other:admin",
        }),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": "Bearer operator"},
    ) as client:
        yield client, settings, tenant, temporal, champion


async def test_solidify_reserves_a_build_and_only_shows_matching_records(api):
    client, settings, tenant, temporal, champion = api
    path = f"/v1/champions/{champion['id']}"
    assert (await client.get(path)).json()["solidify_builds"] == []
    result = await client.post(f"{path}/solidify", json={"notes": "Release"})
    assert result.status_code == 202
    record = result.json()
    assert record["notes"] == "Release"
    assert record["status"] == "queued"
    assert record["base_version"] == champion["agent_version"]
    assert record["ref"] == record["branch"] == f"sca/champion-{champion['id'].hex[:12]}"
    assert record["labels"] == {"kind": "champion-solidify", "champion_id": str(champion["id"]),
                                "source_version": champion["agent_version"]}
    temporal.start_workflow.assert_awaited_once_with(
        ChampionSolidifyWorkflow.run, {"record": record, "created_by": "static-token"},
        id=f"champion-solidify-{record['id']}", task_queue=settings.task_queue,
    )
    reserve_build(settings, str(uuid4()), "other", ref="main", labels=record["labels"])
    reserve_build(settings, str(uuid4()), "eval", ref="main", labels={"kind": "opencode-fix"})
    detail = (await client.get(path)).json()
    assert [build["id"] for build in detail["solidify_builds"]] == [record["id"]]
    tenant.get_champion.side_effect = NoResultFound
    assert (await client.get(path)).status_code == 404
    assert (await client.post(f"{path}/solidify")).status_code == 404


async def test_failed_dispatch_leaves_a_failed_build(api):
    client, _, _, temporal, champion = api
    path = f"/v1/champions/{champion['id']}"
    temporal.start_workflow.side_effect = RuntimeError("unavailable")
    assert (await client.post(f"{path}/solidify")).status_code == 503
    build, = (await client.get(path)).json()["solidify_builds"]
    assert build["status"] == "failed"


async def test_default_requires_a_registered_binary(api):
    client, settings, tenant, _, _ = api
    version = settings.opencode_versions_dir / "2.3.0-sca.42"
    route = f"/v1/agents/opencode/versions/{version.name}/default"
    version.mkdir(parents=True)
    (version / "meta.json").write_text(json.dumps({
        "version": version.name, "source_repo": settings.opencode_repo_url, "commit": "a" * 40,
    }))
    assert (await client.post(route)).status_code == 404
    tenant.set_default_opencode_version.assert_not_called()
    (version / "opencode").touch()
    assert (await client.post(route)).json() == {"version": version.name}
    tenant.set_default_opencode_version.assert_awaited_once_with(version.name)


@pytest.mark.parametrize("user", ["member", "tenant-admin"])
async def test_release_routes_require_the_eval_admin(api, user):
    client, _, tenant, temporal, champion = api
    headers = {"Authorization": f"Bearer {user}"}
    path = f"/v1/champions/{champion['id']}"
    assert (await client.get(path, headers=headers)).status_code == 403
    assert (await client.post(f"{path}/solidify", headers=headers)).status_code == 403
    route = "/v1/agents/opencode/versions/2.3.0-sca.1/default"
    assert (await client.post(route, headers=headers)).status_code == 403
    temporal.start_workflow.assert_not_called()
    tenant.set_default_opencode_version.assert_not_called()
