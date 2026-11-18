from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.core.models import Principal
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


async def test_storage_api_rescore_and_isolation(store, api_store, tmp_path):
    task, _ = await store.for_tenant("eval").create_task(
        tenant_id="eval", kind="coding", spec={}, idempotency_key=None)
    run_id = uuid4()
    await store.create_eval_run({"id": run_id, "tenant_id": "eval", "suite": "opencode-demo",
        "suite_hash": "historic", "variant": {}, "created_by": "test", "status": "completed"})
    await store.record_eval_result({"run_id": run_id, "tenant_id": "eval", "case_id": "chinese-greeting",
        "repeat": 0, "task_id": task.id, "acceptance_passed": True, "verification_passed": True,
        "turns": 1, "tokens": 0, "cost_usd": 0, "duration_s": 1})
    fault = await store.append_event(task.id, "agent.message", {"text": "The edit tool stripped the Chinese text."})
    objects = LocalObjectStore(tmp_path)
    auth = StaticTokenAuthenticator({"admin": "eval:admin", "member": "eval:member", "other": "other:admin"})
    app = create_app(store=api_store, orchestrator=SimpleNamespace(), object_store=objects, authenticator=auth)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                headers={"Authorization": "Bearer admin"}) as client:
        path = f"/v1/evals/runs/{run_id}"
        before = (await client.get(path)).json()
        assert before["results"][0]["trajectory_passed"] is None
        assert before["results"][0]["trajectory_findings"] == []
        assert before["results"][0]["passed"] is True
        for token in ("member", "other"):
            assert (await client.post(path + "/rescore", headers={"Authorization": f"Bearer {token}"})).status_code == 403
        response = await client.post(path + "/rescore")
        assert response.status_code == 200, response.text
        scored = response.json()
        row = scored["results"][0]
        assert row["acceptance_passed"] is True
        assert row["trajectory_passed"] is row["passed"] is False
        assert row["trajectory_findings"][0]["event_seq"] == fault.seq
        assert scored["failed"] == 1 and scored["passed"] == 0
        assert (await client.post(path + "/rescore")).json() == scored
        report = (await client.get(f"/v1/evals/compare?a={run_id}&b={run_id}")).json()
        assert report["A"]["pass_rate"]["mean"] == 0
        assert report["A"]["acceptance_pass_rate"]["mean"] == 1
        assert (await client.post(f"/v1/evals/runs/{uuid4()}/rescore")).status_code == 404
    # The new column grant does not permit crossing the existing tenant RLS policy.
    await api_store.for_tenant("other").update_eval_trajectory(str(run_id), "chinese-greeting", 0, True, [])
    assert (await store.get_eval_run(str(run_id)))["results"][0]["trajectory_passed"] is False


async def test_rescore_scoped_admin(store, api_store, tmp_path):
    class Auth:
        async def authenticate(self, token):
            return Principal(tenant_id="eval", subject="scoped", role="viewer", scopes=["eval:admin"])
    app = create_app(store=api_store, orchestrator=SimpleNamespace(),
                     object_store=LocalObjectStore(tmp_path), authenticator=Auth())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # An allowed scope reaches run lookup; ordinary read authorization rejects this scope.
        response = await client.post(f"/v1/evals/runs/{uuid4()}/rescore")
        assert response.status_code == 404
