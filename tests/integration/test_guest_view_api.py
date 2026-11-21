import socket
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.core.models import TaskMessageInput, TaskState
from sca.storage.local import LocalObjectStore, key_for


def postgres_socket_denied() -> bool:
    try:
        with socket.socket(socket.AF_INET):
            return False
    except PermissionError:
        return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(postgres_socket_denied(), reason="Sandbox denies PostgreSQL sockets"),
]


async def test_guest_reads_demo_history_with_tenant_rls(store, api_store, tmp_path, monkeypatch):
    provider = tmp_path / "provider.env"
    provider.touch()
    monkeypatch.setenv("SCA_PORTAL_LOCAL_CONFIG", str(provider))
    objects = LocalObjectStore(tmp_path / "objects")
    run_id, evolution_id = uuid4(), uuid4()
    eval_store = store.for_tenant("eval")
    task, _ = await eval_store.create_task(
        tenant_id="eval", kind="coding", idempotency_key="fixture-private-retry",
        spec={"instruction": "demo history", "eval_run_id": str(run_id), "credentials": {"canary": True}},
    )
    other, _ = await store.for_tenant("other").create_task(
        tenant_id="other", kind="coding", idempotency_key=None, spec={"instruction": "other history"},
    )
    await eval_store.append_event(task.id, "agent.message", {"text": "finished", "token": "fixture-event"})
    message = await eval_store.add_task_message(
        task.id, TaskMessageInput(text="Review with member@example.org"), "member@example.org",
    )
    await eval_store.add_usage(task.id, {"cost_micro_usd": 1_000})
    await eval_store.set_state(task.id, TaskState.CANCELLED)
    patch = b"+demo change\n+contact = 'member@example.org'\n"
    name = "changes.patch"
    await eval_store.add_artifact(task.id, name, "text/x-diff",
                                  await objects.put(key_for(task.id, name), patch), len(patch))
    await eval_store.create_eval_run({
        "id": run_id, "suite": "fixture", "suite_hash": "fixture-hash", "status": "completed",
        "created_by": "member@example.org", "variant": {"member_email": "member@example.org"},
    })
    await eval_store.create_evolution({
        "id": evolution_id, "status": "completed", "created_by": "member@example.org",
        "config": {"suite": "fixture", "stripe_customer_id": "cus_fixture1234"},
    })

    def client_for(guest_tenant=None):
        settings = Settings(_env_file=None, guest_view="local", guest_tenant=guest_tenant, data_dir=tmp_path)
        monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
        app = create_app(store=api_store, orchestrator=SimpleNamespace(), object_store=objects,
                         authenticator=StaticTokenAuthenticator({"fixture-token": "eval:admin"}))
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async with client_for() as client:
        assert (await client.get("/v1/portal/config")).json() == {
            "guest_view": True, "guest_tenant": "eval", "model_configured": False,
        }
        tasks = (await client.get("/v1/tasks?tenant_id=other")).json()
        assert [row["id"] for row in tasks] == [str(task.id)]
        assert (await client.get(f"/v1/tasks/{other.id}")).status_code == 404
        root = f"/v1/tasks/{task.id}"
        response = await client.get(root)
        assert response.status_code == 200 and response.json()["idempotency_key"] is None
        assert "credentials" not in response.json()["spec"]
        for path in ("/events", "/events/stream"):
            response = await client.get(root + path)
            assert response.status_code == 200 and "finished" in response.text
            assert "fixture-event" not in response.text
        response = await client.get(root + "/dag")
        assert response.status_code == 200
        node = next(node for node in response.json()["nodes"] if node["kind"] == "interjection")
        assert node["meta"]["text"] == "Review with [redacted]" and "actor" not in node["meta"]
        response = await client.get(root + "/messages")
        assert response.status_code == 200 and response.json() == [{
            **message.model_dump(mode="json"), "actor": "[redacted]", "text": "Review with [redacted]",
        }]
        for suffix in ("/dag", "/messages"):
            assert (await client.get(f"/v1/tasks/{other.id}" + suffix)).status_code == 404
        assert (await client.post(root + "/messages", json={"text": "Follow up"})).status_code == 401
        artifact = (await client.get(root + "/artifacts")).json()[0]
        assert artifact["uri"] == root + "/artifacts/changes.patch"
        response = await client.get(artifact["uri"])
        assert response.status_code == 200 and "+demo change" in response.text
        assert "member@example.org" not in response.text
        response = await client.get("/v1/evals/runs")
        assert [row["id"] for row in response.json()] == [str(run_id)]
        assert "member@example.org" not in response.text
        response = await client.get(f"/v1/evolutions/{evolution_id}")
        assert response.status_code == 200 and response.json()["config"] == {"suite": "fixture"}
        usage = (await client.get("/v1/usage")).json()
        assert [row["tenant_id"] for row in usage] == ["eval"]
        for path in ("/v1/tenant/members", "/v1/tenant/billing", "/v1/tokens", "/v1/integrations/scm"):
            assert (await client.get(path)).status_code == 401
        assert (await client.post(root + "/cancel")).status_code == 401
        response = await client.post("/v1/tasks", headers={"Authorization": "Bearer fixture-token"},
                                     json={"kind": "coding", "spec": {}})
        assert response.status_code == 409 and response.json()["error"]["code"] == "model_not_configured"

    async with client_for("other") as client:
        assert [row["id"] for row in (await client.get("/v1/tasks")).json()] == [str(other.id)]
        assert (await client.get(f"/v1/tasks/{task.id}/artifacts")).status_code == 404
        assert (await client.get(f"/v1/evals/runs/{run_id}")).status_code == 403
        assert (await client.get(f"/v1/evolutions/{evolution_id}")).status_code == 403
