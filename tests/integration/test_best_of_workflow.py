"""Temporal orchestration, sandbox forks, and review through the task API."""

import asyncio
import socket
from uuid import uuid4

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from sca.core.models import TaskState
from sca.kinds.coding.kind import CodingKind
from sca.orchestration.client import TemporalOrchestrator, workflow_id
from sca.storage.local import key_for
from tests.best_of_harness import TASK_ID, api_client, make_harness

pytestmark = pytest.mark.integration


@pytest.fixture
async def temporal_env(tmp_path):
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
    except OSError as exc:
        pytest.skip(f"Temporal test server requires a loopback socket ({exc.strerror})")
    try:
        async with asyncio.timeout(25):
            env = await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(tmp_path))
    except (RuntimeError, OSError, TimeoutError) as exc:
        pytest.skip(f"Temporal test server unavailable ({exc})")
    async with env:
        yield env


@pytest.mark.parametrize("policy", ["verify_then_cost", "human"])
async def test_best_of_workflow(temporal_env, tmp_path, best_of_repo, policy):
    harness = make_harness(tmp_path, best_of_repo, concurrency=2, max_cost_usd=.4, auto_finish=True,
                           best_of={"n": 2, "models": ["wide", "cheap"], "select": policy})
    queue = f"best-of-test-{uuid4()}"
    orchestrator = TemporalOrchestrator(temporal_env.client, queue)
    async with (
        Worker(temporal_env.client, task_queue=queue, workflows=CodingKind().workflows(),
               activities=harness.activities),
        api_client(harness, orchestrator) as api,
        asyncio.timeout(30),
    ):
        await orchestrator.start_task(str(TASK_ID), "coding")
        handle = temporal_env.client.get_workflow_handle(workflow_id(str(TASK_ID)))
        if policy == "human":
            await harness.store.states[TaskState.AWAITING_REVIEW].wait()
            path = f"/v1/tasks/{TASK_ID}/best-of"
            view = (await api.get(path)).json()
            assert view["awaiting_selection"] and len(view["attempts"]) == 2
            assert all(a["completed"] and a["verification"]["passed"] for a in view["attempts"])
            assert (await api.post(f"{path}/select", json={"attempt": 1})).status_code == 202
        assert await handle.result() == "SUCCEEDED"
    chosen = 1 if policy == "human" else 2
    assert harness.store.task.result["best_of_chosen"] == chosen
    assert harness.store.task.result["verification_passed"] is True
    assert len(harness.sandbox.setup_calls) == 1
    assert len(harness.sandbox.checks) == 4
    assert await harness.objects.get(key_for(TASK_ID, "changes.patch")) == await harness.objects.get(
        key_for(TASK_ID, f"best-of-{chosen}.patch"))
