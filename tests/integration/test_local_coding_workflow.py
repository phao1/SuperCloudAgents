"""Run a coding task from submission to review with native subprocesses."""

import asyncio
import socket
from uuid import uuid4

import httpx
import pytest
from temporalio.testing import WorkflowEnvironment

from sca.api import app as api_app
from sca.config import Settings
from sca.core.models import EventType, TaskState
from sca.orchestration.client import TemporalOrchestrator, workflow_id
from sca.orchestration.worker import build_worker

pytestmark = pytest.mark.integration


@pytest.fixture
async def local_temporal(tmp_path):
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
    except OSError as exc:
        pytest.skip(f"Temporal test environment requires loopback sockets ({exc.strerror})")
    try:
        async with asyncio.timeout(25):
            env = await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(tmp_path))
    except (RuntimeError, OSError, TimeoutError):
        pytest.skip("Temporal test server is unavailable")
    async with env:
        yield env


async def test_fake_coding_task_with_local_sandbox(
    local_temporal, store, tmp_path, best_of_repo, monkeypatch,
):
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", sandbox_driver="local", agent_backend="fake",
        task_queue=f"source-test-{uuid4()}", maintenance_schedule=False,
    )
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    monkeypatch.setattr("sca.config.get_settings", lambda: settings)
    orchestrator = TemporalOrchestrator(local_temporal.client, settings.task_queue)
    app = api_app.create_app(store=store, orchestrator=orchestrator)
    async with (
        await build_worker(local_temporal.client, settings, store),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer " + settings.api_token},
        ) as client,
        asyncio.timeout(45),
    ):
        response = await client.post("/v1/tasks", json={"kind": "coding", "spec": {
            "repo_url": str(best_of_repo.task_dir),
            "instruction": "Create the result.\n\n```sca-fake\nprintf 'ready\\n' > result.txt\n```",
            "image": "no-such-container-image",
            "setup_command": "command -v git && command -v python3",
            "verify_command": "grep -qx ready result.txt",
        }})
        assert response.status_code == 201, response.text
        task_id = response.json()["id"]
        path = f"/v1/tasks/{task_id}"
        while True:
            task = (await client.get(path)).json()
            if task["state"] == TaskState.AWAITING_REVIEW:
                break
            assert task["state"] not in (TaskState.FAILED, TaskState.CANCELLED), task["state_reason"]
            await asyncio.sleep(0.1)

        sandbox_id = task["result"]["sandbox_id"]
        directory = settings.data_dir / "sandboxes" / sandbox_id
        assert (directory / "workspace" / "result.txt").read_text() == "ready\n"
        assert task["result"]["verification_passed"] is True
        patch = await client.get(path + "/artifacts/changes.patch")
        assert patch.status_code == 200
        assert "+ready" in patch.text
        assert (await client.post(path + "/review", json={"decision": "approve"})).status_code == 200
        handle = local_temporal.client.get_workflow_handle(workflow_id(task_id))
        assert await handle.result() == TaskState.SUCCEEDED
        assert not directory.exists()

    events = await store.list_events(task_id)
    assert any(event.type == EventType.SANDBOX_CREATED and event.payload["driver"] == "local"
               for event in events)
    assert any(event.type == EventType.SANDBOX_DESTROYED for event in events)
