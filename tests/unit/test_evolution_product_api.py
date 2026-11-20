"""Evolution product APIs bind tenant overrides and benchmark publication to their roles."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from temporalio.exceptions import WorkflowAlreadyStartedError

from sca.api import evolutions, harvest
from sca.api.authz import ForbiddenError, principal
from sca.core.models import Principal
from sca.evolve.scheduler import schedule_workflow_id


@pytest.fixture
async def api(tmp_path, monkeypatch):
    settings = SimpleNamespace(eval_suites_dir=tmp_path, task_queue="queue")
    monkeypatch.setattr(evolutions, "get_settings", lambda: settings)
    monkeypatch.setattr(harvest, "get_settings", lambda: settings)
    (tmp_path / "suite").mkdir()
    monkeypatch.setattr(evolutions, "load_suite", lambda _: {
        "cases": [{"spec": {}, "split": split} for split in ("dev", "holdout")],
    })
    scopes = {}
    for tenant in ("eval", "team"):
        scopes[tenant] = SimpleNamespace(
            tenant_id=tenant, list_champion_rollouts=AsyncMock(return_value=[]),
            put_champion_rollout=AsyncMock(return_value={"tenant_id": tenant, "percent": 30}),
            rollback_champion=AsyncMock(return_value={"active": True}),
            create_evolution_schedule=AsyncMock(return_value={"tenant_id": tenant}),
            get_evolution_schedule=AsyncMock(return_value={"tenant_id": tenant}),
            list_evolution_schedules=AsyncMock(return_value=[]),
            update_evolution_schedule=AsyncMock(return_value={"tenant_id": tenant}),
            delete_evolution_schedule=AsyncMock(),
            list_harvest_candidates=AsyncMock(return_value=[]),
            review_harvest_candidate=AsyncMock(return_value={"tenant_id": tenant}),
        )
    app = FastAPI()
    app.include_router(evolutions.router)
    app.include_router(harvest.router)
    app.state.store = SimpleNamespace(for_tenant=lambda tenant: scopes[tenant])
    temporal = SimpleNamespace(start_workflow=AsyncMock())
    app.state.orchestrator = SimpleNamespace(client=temporal)
    user = Principal(tenant_id="team", subject="operator", role="admin")

    def get_user():
        return user

    app.dependency_overrides[principal] = get_user

    async def forbidden(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    app.add_exception_handler(ForbiddenError, forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http, user, scopes, temporal


async def test_rollout_is_tenant_admin_scoped_and_rollback_is_platform_only(api):
    http, user, scopes, _ = api
    champion_id = uuid4()
    body = {"agent_version": "v", "model": "m", "percent": 30, "champion_id": str(champion_id)}
    path = "/v1/champions/rollouts"
    response = await http.put(path, json=body)
    assert response.json() == {"tenant_id": "team", "percent": 30}
    scopes["team"].put_champion_rollout.assert_awaited_once_with("v", "m", champion_id, 30, "operator")
    assert (await http.get(path)).status_code == 200
    assert (await http.put(path, json={**body, "percent": 101})).status_code == 422
    assert (await http.put(path, json={**body, "tenant_id": "eval"})).status_code == 422
    rollback = f"/v1/champions/{champion_id}/rollback"
    assert (await http.post(rollback)).status_code == 403
    user.tenant_id = "eval"
    assert (await http.post(rollback)).json() == {"active": True}
    scopes["eval"].rollback_champion.assert_awaited_once_with(champion_id, "operator")
    user.role = "member"
    assert (await http.get(path)).status_code == 403


async def test_schedule_crud_and_run_now(api):
    http, user, scopes, temporal = api
    path = "/v1/evolution-schedules"
    body = {"suite": "suite", "agent_version": "v", "interval_hours": 8, "threshold": .8,
            "auto_rollout_percent": 25, "config": {"concurrency": 2}}
    assert (await http.post(path, json=body)).status_code == 403
    user.tenant_id = "eval"
    assert (await http.post(path, json=body)).status_code == 201
    saved = scopes["eval"].create_evolution_schedule.call_args.args[0]
    assert saved["split"] == "dev"
    assert saved["config"]["concurrency"] == 2
    assert (await http.post(path, json={**body, "split": "holdout"})).status_code == 422
    assert (await http.get(path)).json() == []
    schedule_id = uuid4()
    detail = f"{path}/{schedule_id}"
    assert (await http.get(detail)).status_code == 200
    assert (await http.put(detail, json={**body, "enabled": False})).status_code == 200
    assert scopes["eval"].update_evolution_schedule.call_args.args[1]["enabled"] is False
    response = await http.post(detail + "/run-now")
    assert response.status_code == 202
    assert response.json()["workflow_id"] == schedule_workflow_id(schedule_id)
    assert temporal.start_workflow.call_args.args[1] == {"schedule_id": str(schedule_id), "force": True}
    temporal.start_workflow.side_effect = WorkflowAlreadyStartedError(
        schedule_workflow_id(schedule_id), "EvolutionSchedulerWorkflow",
    )
    assert (await http.post(detail + "/run-now")).status_code == 409
    assert (await http.delete(detail)).status_code == 204


async def test_harvest_intake_and_publication_have_separate_scope(api, monkeypatch):
    http, user, scopes, _ = api
    submit = AsyncMock(return_value={"status": "pending"})
    monkeypatch.setattr(harvest, "harvest", submit)
    path = "/v1/evals/harvest"
    task_id, candidate_id = uuid4(), uuid4()
    assert (await http.post(path, json={"task_id": str(task_id), "split": "dev"})).status_code == 201
    assert submit.call_args.args[1] is scopes["team"]
    assert (await http.get(path + "?tenant_id=eval")).status_code == 403
    assert (await http.post(f"{path}/{candidate_id}/approve")).status_code == 403
    assert (await http.post(f"{path}/{candidate_id}/reject")).status_code == 200
    scopes["team"].review_harvest_candidate.assert_awaited_once_with(candidate_id, "rejected", "operator")
    user.tenant_id = "eval"
    assert (await http.get(path + "?tenant_id=team")).status_code == 200
    assert (await http.post(f"{path}/{candidate_id}/approve?tenant_id=team")).status_code == 200
    assert scopes["team"].review_harvest_candidate.call_args.args[:3] == (candidate_id, "approved", "operator")
