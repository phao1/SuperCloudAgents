import asyncio
from uuid import uuid4

import httpx
import pytest

pytest.importorskip("sca.kinds.coding.spec")

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.core.models import TaskState
from sca.storage.local import LocalObjectStore, key_for

pytestmark = pytest.mark.integration


class FakeOrchestrator:
    def __init__(self, store):
        self.store = store
        self.starts = []
        self.reviews = []
        self.ci = []
        self.fail_start = False

    async def start_task(self, task_id, kind):
        assert (await self.store.list_events(task_id))[0].type == "task.created"
        if self.fail_start:
            raise RuntimeError("unavailable")
        self.starts.append((task_id, kind))

    async def submit_review(self, task_id, decision, feedback):
        assert (await self.store.list_events(task_id))[-1].type == "review.submitted"
        self.reviews.append((task_id, decision, feedback))

    async def submit_ci_result(self, task_id, commit, passed, summary, url):
        self.ci.append((task_id, commit, passed, summary, url))

    async def pause_task(self, task_id):
        self.paused = task_id

    async def resume_task(self, task_id):
        await self.store.set_state(task_id, TaskState.RUNNING)

    async def cancel_task(self, task_id):
        await self.store.set_state(task_id, TaskState.CANCELLED)


@pytest.fixture
async def api(store, api_store, tmp_path):
    orchestrator = FakeOrchestrator(store)
    objects = LocalObjectStore(tmp_path)
    app = create_app(
        store=api_store,
        orchestrator=orchestrator,
        object_store=objects,
        authenticator=StaticTokenAuthenticator({"token": "one", "other": "two"}),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer token"},
    ) as client:
        yield client, orchestrator, objects


async def submit(client, key=None):
    return await client.post(
        "/v1/tasks",
        json={"kind": "coding", "spec": {"repo_url": "/tmp/repo", "instruction": "fix it"}},
        headers={"Idempotency-Key": key} if key else {},
    )


async def awaiting(store, task_id):
    for state in (
        TaskState.PROVISIONING,
        TaskState.RUNNING,
        TaskState.VERIFYING,
        TaskState.AWAITING_REVIEW,
    ):
        await store.set_state(task_id, state)


def error(response, status):
    assert response.status_code == status, response.text
    assert set(response.json()["error"]) == {"code", "message"}


async def test_routes(api, store):
    client, orch, objects = api
    assert (await client.get("/healthz", headers={"Authorization": ""})).status_code == 200
    assert (await client.get("/")).status_code == 200
    response = await submit(client, "idempotent")
    assert response.status_code == 201
    task = response.json()
    task_id = task["id"]
    assert task["spec"]["base_ref"] == "main"
    assert (await submit(client, "idempotent")).json()["id"] == task_id
    assert len(orch.starts) == 2
    assert len((await client.get("/v1/tasks")).json()) == 1
    assert (await client.get("/v1/tasks?state=FAILED")).json() == []
    assert (await client.get("/v1/tasks/" + task_id)).json()["id"] == task_id
    assert len((await client.get(f"/v1/tasks/{task_id}/events")).json()) == 1
    assert (await client.get(f"/v1/tasks/{task_id}/events?after=1")).json() == []
    assert (await client.get(f"/v1/tasks/{task_id}/artifacts")).json() == []
    uri = await objects.put(key_for(task_id, "changes.patch"), b"diff")
    await store.add_artifact(task_id, "changes.patch", "text/plain", uri, 4)
    assert len((await client.get(f"/v1/tasks/{task_id}/artifacts")).json()) == 1
    assert (await client.get(f"/v1/tasks/{task_id}/artifacts/changes.patch")).content == b"diff"
    await awaiting(store, task_id)
    for decision, feedback in [
        ("approve", None),
        ("request_changes", "fix more"),
        ("reject", "no"),
    ]:
        response = await client.post(
            f"/v1/tasks/{task_id}/review", json={"decision": decision, "feedback": feedback}
        )
        assert response.status_code == 200
        assert orch.reviews[-1] == (task_id, decision, feedback)
    response = await client.post(f"/v1/tasks/{task_id}/cancel")
    assert response.status_code == 200
    assert response.json()["state"] == "CANCELLED"


async def test_errors_and_tenants(api, store):
    client, _, _ = api
    task_id = (await submit(client)).json()["id"]
    for authorization in ("", "Bearer bad", "Basic token"):
        error(await client.get("/v1/tasks", headers={"Authorization": authorization}), 401)
    for body in ({"kind": "unknown", "spec": {}}, {"kind": "coding", "spec": {}}, {}):
        error(await client.post("/v1/tasks", json=body), 422)
    for query in ("state=bad", "limit=0", "limit=501"):
        error(await client.get("/v1/tasks?" + query), 422)
    error(await client.get(f"/v1/tasks/{task_id}/events?after=-1"), 422)
    error(
        await client.get(
            f"/v1/tasks/{task_id}/events/stream", headers={"Last-Event-ID": "invalid"}
        ),
        422,
    )
    error(await client.post(f"/v1/tasks/{task_id}/review", json={"decision": "approve"}), 409)
    await awaiting(store, task_id)
    for body in (
        {"decision": "bad"},
        {"decision": "request_changes"},
        {"decision": "request_changes", "feedback": "  "},
    ):
        error(await client.post(f"/v1/tasks/{task_id}/review", json=body), 422)
    error(await client.get(f"/v1/tasks/{task_id}/artifacts/missing"), 404)
    for suffix, method in [
        ("", "get"),
        ("/events", "get"),
        ("/events/stream", "get"),
        ("/artifacts", "get"),
        ("/artifacts/changes.patch", "get"),
        ("/cancel", "post"),
        ("/review", "post"),
    ]:
        kwargs = {"json": {"decision": "approve"}} if suffix == "/review" else {}
        error(await getattr(client, method)(f"/v1/tasks/{uuid4()}{suffix}", **kwargs), 404)
        error(
            await getattr(client, method)(
                f"/v1/tasks/{task_id}{suffix}", headers={"Authorization": "Bearer other"}, **kwargs
            ),
            404,
        )
    assert (await client.get("/v1/tasks", headers={"Authorization": "Bearer other"})).json() == []
    await store.set_state(task_id, TaskState.SUCCEEDED)
    error(await client.post(f"/v1/tasks/{task_id}/cancel"), 409)
    error(await client.post(f"/v1/tasks/{task_id}/review", json={"decision": "approve"}), 409)


async def test_submit_retry(api):
    client, orch, _ = api
    orch.fail_start = True
    with pytest.raises(RuntimeError):
        await submit(client, "retry")
    task = (await client.get("/v1/tasks")).json()[0]
    orch.fail_start = False
    assert (await submit(client, "retry")).json()["id"] == task["id"]


async def test_sse_closes_and_resumes(api, store):
    client, _, _ = api
    task_id = (await submit(client)).json()["id"]

    async def finish():
        await asyncio.sleep(0.1)
        await store.append_event(task_id, "agent.message", {"text": "hello"})
        await store.set_state(task_id, TaskState.CANCELLED)

    async with asyncio.TaskGroup() as group:
        group.create_task(finish())
        response = await asyncio.wait_for(client.get(f"/v1/tasks/{task_id}/events/stream"), 7)
    assert response.status_code == 200
    assert "id: 1\nevent: task.created\ndata: " in response.text
    assert "hello" in response.text
    assert "CANCELLED" in response.text
    response = await client.get(
        f"/v1/tasks/{task_id}/events/stream?after=0", headers={"Last-Event-ID": "2"}
    )
    assert "id: 1\n" not in response.text and "id: 2\n" not in response.text
    assert "id: 3\n" in response.text
    response = await client.get(f"/v1/tasks/{task_id}/events/stream?after=3")
    assert response.text == ""


async def test_pause_resume(api, store):
    client, orch, _ = api
    task_id = (await submit(client)).json()["id"]
    assert (await client.post(f"/v1/tasks/{task_id}/pause")).json()["state"] == "QUEUED"
    assert orch.paused == task_id
    error(await client.post(f"/v1/tasks/{task_id}/resume"), 409)
    await store.set_state(task_id, TaskState.PROVISIONING)
    await store.set_state(task_id, TaskState.PAUSED)
    error(await client.post(f"/v1/tasks/{task_id}/pause"), 409)
    assert (await client.post(f"/v1/tasks/{task_id}/resume")).json()["state"] == "RUNNING"
    await store.set_state(task_id, TaskState.CANCELLED)
    error(await client.post(f"/v1/tasks/{task_id}/pause"), 409)


async def test_metrics(api):
    client, _, _ = api
    await submit(client)
    response = await client.get("/metrics", headers={"Authorization": ""})
    assert response.status_code == 200
    assert 'sca_tasks{state="QUEUED"} 1.0' in response.text
    assert "sca_tasks_created_total 1.0" in response.text
    assert "/v1/tasks" in response.text


async def test_stream_notified(api, store):
    import time

    client, _, _ = api
    task_id = (await submit(client)).json()["id"]
    app = client._transport.app
    listener = app.state.event_listener
    runner = asyncio.create_task(listener.run())
    try:
        async with asyncio.timeout(3):
            while not listener.connected:
                await asyncio.sleep(0.01)

        async def finish():
            await asyncio.sleep(0.1)
            await store.append_event(task_id, "agent.message", {"text": "notified"})
            await store.set_state(task_id, TaskState.CANCELLED)

        started = time.monotonic()
        async with asyncio.TaskGroup() as group:
            group.create_task(finish())
            response = await asyncio.wait_for(client.get(f"/v1/tasks/{task_id}/events/stream"), 2)
        assert "notified" in response.text
        assert time.monotonic() - started < 2
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner


async def test_ci_result_is_forwarded_and_refused_for_finished_tasks(api, store):
    client, orch, _ = api
    task_id = (await submit(client)).json()["id"]
    body = {"commit": "abc1234", "passed": False, "summary": "lint failed", "url": "http://ci/1"}
    response = await client.post(f"/v1/tasks/{task_id}/ci-result", json=body)
    assert response.status_code == 200
    assert orch.ci == [(task_id, "abc1234", False, "lint failed", "http://ci/1")]
    assert (await client.post(f"/v1/tasks/{task_id}/ci-result", json={})).status_code == 422
    await store.set_state(task_id, TaskState.CANCELLED)
    assert (await client.post(f"/v1/tasks/{task_id}/ci-result", json=body)).status_code == 409


async def test_admission_priority_contract(api):
    client, _, _ = api
    body = {"kind": "coding", "spec": {"repo_url": "/tmp/repo", "instruction": "fix it"},
            "priority": "eval"}
    response = await client.post("/v1/tasks", json=body)
    assert response.status_code == 201
    assert response.json()["spec"]["priority"] == "eval"
    assert response.json()["state"] == "QUEUED"
    body["priority"] = "unknown"
    assert (await client.post("/v1/tasks", json=body)).status_code == 422
    schema = (await client.get("/openapi.json")).json()
    submission = schema["components"]["schemas"]["Submission"]
    assert submission["properties"]["priority"]["default"] == "interactive"
