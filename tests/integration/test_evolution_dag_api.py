import socket
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.storage.local import LocalObjectStore
from scripts.evo_dag_demo import seed_demo

pytestmark = pytest.mark.integration


@pytest.fixture
def postgres():
    try:
        with socket.create_connection(("localhost", 5432), timeout=.5):
            pass
    except OSError as exc:
        pytest.skip(f"PostgreSQL is unavailable to the sandbox ({exc.strerror})")


async def test_evolution_dag_and_guest_access_with_rls(postgres, store, api_store, tmp_path, monkeypatch):
    evolution_id = await seed_demo(store.sessionmaker.kw["bind"])
    eval_store = store.for_tenant("eval")
    row = await eval_store.get_evolution(str(evolution_id))
    champion_id = row["best_champion_id"]
    run_id = next(entry["id"] for entry in row["journal"] if entry.get("type") == "evaluation")
    outsider_id, outsider_run_id = uuid4(), uuid4()
    outsider_store = store.for_tenant("other")
    await outsider_store.create_evolution({
        "id": outsider_id, "status": "completed", "config": {"suite": "other"}, "created_by": "other",
    })
    await outsider_store.create_eval_run({
        "id": outsider_run_id, "tenant_id": "other", "suite": "other", "suite_hash": "other",
        "status": "completed", "created_by": "other", "variant": {"evolution_id": str(evolution_id)},
    })
    outsider_task, _ = await outsider_store.create_task(
        tenant_id="other", kind="coding", idempotency_key=None, spec={"eval_run_id": str(run_id)},
    )
    await eval_store.record_eval_result({
        "run_id": run_id, "tenant_id": "eval", "case_id": "other", "repeat": 0, "task_id": outsider_task.id,
        "acceptance_passed": True, "verification_passed": True, "turns": 1, "tokens": 1,
        "cost_usd": .01, "duration_s": 1,
    })
    live_task, _ = await eval_store.create_task(
        tenant_id="eval", kind="coding", idempotency_key=f"eval:{run_id}:in-progress:0",
        spec={"eval_run_id": str(run_id), "agent_backend": "fake"},
    )
    await eval_store.add_usage(live_task.id, {"cost_micro_usd": 3200})
    completion = uuid4()
    api_eval = api_store.for_tenant("eval")
    await api_eval.record_evolution_proposer_spend(evolution_id, completion, .004)
    await api_eval.record_evolution_proposer_spend(evolution_id, completion, .004)
    await outsider_store.record_evolution_proposer_spend(outsider_id, uuid4(), .90)

    def client_for(guest_view="local", guest_tenant="eval"):
        settings = Settings(_env_file=None, guest_view=guest_view, guest_tenant=guest_tenant, data_dir=tmp_path)
        monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
        monkeypatch.setattr("sca.api.agents.get_settings", lambda: settings)
        app = create_app(
            store=api_store, orchestrator=SimpleNamespace(), object_store=LocalObjectStore(tmp_path),
            authenticator=StaticTokenAuthenticator({"admin": "eval:admin", "reader": "eval:viewer",
                                                    "outside": "other:admin"}),
        )
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    evolution = f"/v1/evolutions/{evolution_id}"
    champion = f"/v1/champions/{champion_id}"
    eval_run = f"/v1/evals/runs/{run_id}"
    admin = {"Authorization": "Bearer admin"}
    async with client_for() as client:
        for path in ("/v1/evolutions", evolution, evolution + "/dag", "/v1/champions", champion,
                     "/v1/evals/runs", eval_run):
            response = await client.get(path, params={"tenant_id": "other"})
            assert response.status_code == 200 and "created_by" not in response.text
        response = await client.get(evolution + "/dag")
        assert str(outsider_task.id) not in response.text and str(outsider_run_id) not in response.text
        nodes = response.json()["nodes"]
        root = next(node for node in nodes if node["kind"] == "evolution")
        assert root["cost_usd"] == pytest.approx(float(row["spent_usd"]) + .0032 + .004)
        assert root["meta"]["proposer_spent_usd"] == pytest.approx(.004)
        assert sum(node["kind"] == "generation" for node in nodes) == 2
        assert sum(node["kind"] == "candidate" for node in nodes) == 6
        assert next(node for node in nodes if node["kind"] == "champion")["meta"]["champion_id"] == str(champion_id)
        live = next(node for node in nodes if node["task_id"] == str(live_task.id))
        assert live["status"] == "queued" and live["meta"]["case_id"] == "in-progress"
        assert (await client.get(f"/v1/tasks/{live_task.id}/dag")).status_code == 200
        assert (await client.get(evolution + "/dag", headers=admin)).json() == response.json()
        assert (await client.get(f"/v1/evolutions/{outsider_id}/dag")).status_code == 404
        assert (await client.get(evolution + "/dag", headers={"Authorization": "Bearer outside"})).status_code == 403
        for method, path in [
            ("POST", "/v1/evolutions"), ("POST", evolution + "/stop"), ("POST", evolution + "/approve"),
            ("POST", evolution + "/reject"), ("POST", champion + "/activate"), ("POST", champion + "/solidify"),
            ("POST", champion + "/rollback"), ("PUT", "/v1/champions/rollouts"),
            ("POST", "/v1/evolution-schedules"), ("GET", "/v1/evolution-schedules"),
            ("POST", f"/v1/evolution-schedules/{outsider_id}/run-now"),
            ("DELETE", f"/v1/evolution-schedules/{outsider_id}"),
        ]:
            assert (await client.request(method, path, json={})).status_code == 401
            assert (await client.request(method, path, json={}, headers={"Authorization": "Bearer reader"})).status_code == 403

    async with client_for(guest_view="off") as client:
        assert (await client.get(evolution + "/dag")).status_code == 401
    async with client_for(guest_tenant="other") as client:
        for path in (evolution, evolution + "/dag", eval_run, champion):
            assert (await client.get(path)).status_code == 403
