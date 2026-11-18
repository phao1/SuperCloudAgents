import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from test_opencode import make_task, settings

from sca.api import agents
from sca.api.auth import StaticTokenAuthenticator
from sca.api.authz import ForbiddenError
from sca.core.errors import NotFoundError
from sca.core.models import Principal


@pytest.fixture
def api(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    monkeypatch.setattr(agents, "get_settings", lambda: cfg)
    task = make_task()
    store = AsyncMock()
    store.get_task.return_value = task
    store.list_events.return_value = []
    store.create_task.return_value = (task, True)
    parent = SimpleNamespace(for_tenant=lambda tenant: store)
    app = FastAPI()
    app.state.store = parent
    app.state.authenticator = StaticTokenAuthenticator({"token": "t"})
    app.state.orchestrator = SimpleNamespace(
        start_task=AsyncMock(), client=SimpleNamespace(start_workflow=AsyncMock())
    )
    app.include_router(agents.router)
    for error, status in [(NotFoundError, 404), (ForbiddenError, 403)]:

        async def handler(request, exc, status=status):
            return JSONResponse({"error": str(exc)}, status_code=status)

        app.add_exception_handler(error, handler)
    return app, cfg, store, task


async def test_routes(api):
    app, cfg, store, task = api
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer token"},
    ) as client:
        assert (await client.get("/v1/agents/opencode/versions")).json() == []
        response = await client.post(
            "/v1/agents/opencode/fixes",
            json={"failing_task_id": str(task.id), "agent_backend": "codex"},
        )
        assert response.status_code == 201
        assert store.create_task.call_args.kwargs["spec"]["push"]
        response = await client.post("/v1/agents/opencode/builds", json={"ref": "main"})
        assert response.status_code == 202
        record = response.json()
        assert record["version"] == "2.3.0-sca.1"
        assert app.state.orchestrator.client.start_workflow.call_count == 1
        assert len((await client.get("/v1/agents/opencode/builds")).json()) == 1
        detail = await client.get(f"/v1/agents/opencode/builds/{record['id']}")
        assert detail.json()["log_tail"] == ""
        path = cfg.opencode_versions_dir / "builds" / f"{record['id']}.json"
        data = json.loads(path.read_text())
        data["tenant_id"] = "other"
        path.write_text(json.dumps(data))
        assert (await client.get(f"/v1/agents/opencode/builds/{record['id']}")).status_code == 404
        assert (await client.get(f"/v1/agents/opencode/builds/{uuid4()}")).status_code == 404
        assert (
            await client.post("/v1/agents/opencode/builds", json={"ref": "--evil"})
        ).status_code == 422


async def test_viewer_cannot_mutate(api):
    app, _cfg, _store, task = api

    class Viewer:
        async def authenticate(self, token):
            return Principal(tenant_id="t", subject="viewer", role="viewer")

    app.state.authenticator = Viewer()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/v1/agents/opencode/builds", json={})).status_code == 403
        assert (
            await client.post(
                "/v1/agents/opencode/fixes",
                json={"failing_task_id": str(task.id), "agent_backend": "codex"},
            )
        ).status_code == 403
