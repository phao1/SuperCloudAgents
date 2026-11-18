import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from sca.api import agents
from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.core.models import TaskState

pytestmark = pytest.mark.integration


async def test_fix_linkage_api(store, api_store, tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, data_dir=tmp_path)
    monkeypatch.setattr(agents, "get_settings", lambda: cfg)
    client_workflow = SimpleNamespace(start_workflow=AsyncMock())
    orchestrator = SimpleNamespace(start_task=AsyncMock(), client=client_workflow)
    app = create_app(store=api_store, orchestrator=orchestrator,
                     authenticator=StaticTokenAuthenticator({"token": "one", "other": "two"}))
    source, _ = await store.create_task(tenant_id="one", kind="coding", spec={
        "instruction": "test", "eval_run_id": "run", "eval_case_id": "unicode",
        "agent_version": "2.3.0-sca.1",
    }, idempotency_key=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                headers={"Authorization": "Bearer token"}) as client:
        response = await client.post("/v1/agents/opencode/fixes", json={
            "failing_task_id": str(source.id), "agent_backend": "codex",
        })
        assert response.status_code == 201, response.text
        fix = response.json()
        detail = (await client.get(f"/v1/tasks/{fix['id']}")).json()
        assert detail["spec"]["labels"]["source_task_id"] == str(source.id)
        await store.set_state(fix["id"], TaskState.FAILED)
        # A completed, pushed fix is the build API contract.
        from sqlalchemy import text
        async with store._transaction() as session:
            await session.execute(text("UPDATE tasks SET state='SUCCEEDED', result=:result "
                                       "WHERE id=:id"),
                                  {"id": fix["id"], "result": json.dumps({"commit": "a" * 40,
                                                                          "branch": "fix/test"})})
        response = await client.post("/v1/agents/opencode/builds", json={"task_id": fix["id"]})
        assert response.status_code == 202, response.text
        build = response.json()
        assert build["labels"] == detail["spec"]["labels"]
        log = cfg.opencode_versions_dir / "builds" / f"{build['id']}.log"
        log.write_bytes(b"x" * 9000)
        response = await client.get(f"/v1/agents/opencode/builds/{build['id']}")
        assert len(response.json()["log_tail"]) == 8192
        rows = (await client.get("/v1/agents/opencode/fixes")).json()
        assert len(rows) == 1 and rows[0]["id"] == fix["id"]
        assert rows[0]["builds"][0]["version"] == build["version"]
        assert rows[0]["branch"] == "fix/test"
        assert (await client.get("/v1/agents/opencode/fixes",
                                 headers={"Authorization": "Bearer other"})).json() == []
