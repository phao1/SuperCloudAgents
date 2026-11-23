"""Run a coding task from submission to review with native subprocesses."""

import asyncio
import socket
import subprocess
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


@pytest.fixture
async def local_client(local_temporal, store, tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", sandbox_driver="local", agent_backend="fake",
        task_queue=f"source-test-{uuid4()}", maintenance_schedule=False,
        model_gateway_url="http://127.0.0.1:1",
        api_tokens="fixture-token:eval:admin", api_token="fixture-token", eval_api_token="fixture-token",
        portal_require_token=False,
    )
    demo_config = tmp_path / "demo-config"
    demo_config.write_text("DEEPSEEK_API_KEY=REPLACE_ME\n", encoding="utf-8")
    monkeypatch.setenv("SCA_PORTAL_LOCAL_CONFIG", str(demo_config))
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)
    monkeypatch.setattr("sca.config.get_settings", lambda: settings)
    orchestrator = TemporalOrchestrator(local_temporal.client, settings.task_queue)
    app = api_app.create_app(store=store, orchestrator=orchestrator, local_portal_port=8000)
    async with (
        await build_worker(local_temporal.client, settings, store),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000",
            headers={"Origin": "http://127.0.0.1:8000"},
        ) as client,
        asyncio.timeout(45),
    ):
        yield client, settings, local_temporal


async def test_fake_coding_task_with_local_sandbox(local_client, best_of_repo, store):
    client, settings, local_temporal = local_client
    response = await client.post("/v1/tasks", json={"kind": "coding", "spec": {
        "repo_url": str(best_of_repo.task_dir),
        "agent_backend": "fake",
        "instruction": "Create the result.\n\n```sca-fake\nprintf 'ready\\n' > result.txt\n```",
        "image": "no-such-container-image",
        "memory_enabled": False,
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


@pytest.mark.parametrize("auto_finish", [False, True])
async def test_new_project_delivers_real_commit_to_host(local_client, tmp_path, auto_finish):
    client, _, local_temporal = local_client
    output = tmp_path / "Flask shop"
    response = await client.post("/v1/tasks", json={"kind": "coding", "spec": {
        "output_dir": str(output), "agent_backend": "fake", "plan": "none",
        "instruction": "Create app files\n\n```sca-fake\nprintf 'ready\n' > app.txt\n```",
        "verify_command": "grep -qx ready app.txt", "memory_enabled": False,
        "auto_finish": auto_finish,
    }})
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    path = f"/v1/tasks/{task_id}"
    if not auto_finish:
        while True:
            task = (await client.get(path)).json()
            if task["state"] == TaskState.AWAITING_REVIEW:
                break
            assert task["state"] not in (TaskState.FAILED, TaskState.CANCELLED), task["state_reason"]
            await asyncio.sleep(0.1)
        assert not (output / "app.txt").exists()
        assert (await client.post(path + "/review", json={"decision": "approve"})).status_code == 200

    handle = local_temporal.client.get_workflow_handle(workflow_id(task_id))
    assert await handle.result() == TaskState.SUCCEEDED
    task = (await client.get(path)).json()
    assert task["state"] == TaskState.SUCCEEDED
    assert task["result"]["output_dir"] == output.as_posix()
    assert (output / "app.txt").read_text() == "ready\n"
    commit = (await asyncio.to_thread(
        subprocess.run,
        ["git", "-C", str(output), "log", "-1", "--format=%H%n%an%n%s"],
        check=True, capture_output=True, encoding="utf-8", timeout=15,
    )).stdout.splitlines()
    assert commit == [task["result"]["output_commit"], "SCA Agent", "Create app files"]
    chat = (await client.get(path + "/conversation")).json()["items"]
    item, = [item for item in chat if item["kind"] == "output.written"]
    assert item["text"] == f"代码已写入 {output.as_posix()}（提交 {commit[0][:7]}）"
    dag = (await client.get(path + "/dag")).json()
    node, = [node for node in dag["nodes"] if node["kind"] == "output"]
    assert node["id"] == item["dag_node_id"]
