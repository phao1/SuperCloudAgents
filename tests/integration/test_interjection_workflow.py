"""Enter the task workflow via real Temporal signals and activity cancellation."""

import asyncio
import socket
from uuid import uuid4

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from sca.core.models import TaskMessageInput
from sca.kernel.interjection import HEARTBEAT_INTERVAL, Guidance
from sca.kinds.coding.workflow import CodingTaskWorkflow
from tests.interjection_harness import (
    GUIDANCE_HEADER,
    GatewayInterjectionScript,
    InterjectionScript,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def env(tmp_path):
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
    except OSError:
        pytest.skip("Temporal test environment requires a loopback socket")
    try:
        async with asyncio.timeout(25):
            server = await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(tmp_path))
    except (RuntimeError, OSError, TimeoutError):
        pytest.skip("Temporal test server is unavailable")
    async with server:
        yield server


def worker(env, script, queue):
    return Worker(
        env.client, task_queue=queue, workflows=[CodingTaskWorkflow],
        activities=list(script.activities().values()),
        max_heartbeat_throttle_interval=HEARTBEAT_INTERVAL,
    )


async def start(env, queue, task_id="task"):
    return await env.client.start_workflow(
        CodingTaskWorkflow.run, task_id, id=str(uuid4()), task_queue=queue,
    )


@pytest.mark.parametrize("mode", ["queue", "interrupt"])
async def test_signal_during_a_turn(env, mode):
    script = InterjectionScript(block=True)
    queue = str(uuid4())
    async with worker(env, script, queue), asyncio.timeout(15):
        handle = await start(env, queue)
        await script.turn_entered.wait()
        first = script.add("Address empty inputs")
        second = script.add("Update the changelog")
        await handle.signal(CodingTaskWorkflow.user_message, args=[second, "queue"])
        await handle.signal(CodingTaskWorkflow.user_message, args=[first, mode])
        if mode == "queue":
            script.release.set()
        assert await handle.result() == "SUCCEEDED"
    assert script.turns[-1].prompt.startswith(
        GUIDANCE_HEADER + "\n\nAddress empty inputs\n\nUpdate the changelog\n\n",
    )
    assert [turn.turn for turn in script.turns] == [1, 2]
    assert script.applied == [first, second]
    if mode == "interrupt":
        assert script.steps[1:4] == [("cancelled", 1), ("checkpoint", 1), ("start", 2)]
        assert script.turns[-1].guidance.interrupted_ids == [first]
        assert script.verify_turns == [2]
    else:
        assert script.turns[-1].session_id == "session-1"
        assert script.verify_turns == [1, 2]


async def test_signal_during_review(env):
    script = InterjectionScript(auto_finish=False)
    queue = str(uuid4())
    async with worker(env, script, queue), asyncio.timeout(15):
        handle = await start(env, queue)
        assert await script.reviewed.get() == 1
        message = script.add("Add another example")
        await handle.signal(CodingTaskWorkflow.user_message, args=[message, "queue"])
        assert await script.reviewed.get() == 2
        await handle.signal(CodingTaskWorkflow.submit_review, args=["approve", None])
        assert await handle.result() == "SUCCEEDED"
    assert script.publish_turns == [1, 2]
    assert "Add another example" in script.turns[-1].prompt


async def test_gateway_turn_restarts_before_spend_settles(env, store, tmp_path):
    row, _ = await store.create_task(tenant_id="default", kind="coding", spec={"max_cost_usd": .18},
                                     idempotency_key=None)
    script = GatewayInterjectionScript(tmp_path, store)
    queue = str(uuid4())
    async with worker(env, script, queue), asyncio.timeout(20):
        handle = await start(env, queue, str(row.id))
        await script.turn_entered.wait()
        message = await store.add_task_message(
            row.id, TaskMessageInput(text="Test the empty case", mode="interrupt"), "operator",
        )
        script.pending[str(message.id)] = Guidance(str(message.id), message.text)
        await handle.signal(CodingTaskWorkflow.user_message, args=[str(message.id), "interrupt"])
        await asyncio.wait_for(script.second_turn_entered.wait(), 3)
        assert [cap for _, cap in script.gateway.issued] == [.12, .06]
        script.gateway.release_spend.set()
        assert await handle.result() == "SUCCEEDED"
    task = await store.get_task(row.id)
    assert task.usage["cost_micro_usd"] == 50_000
    events = await store.list_events(row.id)
    interrupted = next(ev.seq for ev in events if ev.type == "agent.turn_interrupted")
    delivery = next(ev.seq for ev in events if ev.type == "user.message.delivered")
    charge = next(ev.seq for ev in events if ev.type == "agent.usage" and ev.payload["turn"] == 1)
    assert interrupted < delivery < charge
    await Replayer(workflows=[CodingTaskWorkflow]).replay_workflow(await handle.fetch_history())


async def test_replay_history_without_the_interjection_patch(env):
    script = InterjectionScript(legacy=True)
    queue = str(uuid4())
    async with worker(env, script, queue), asyncio.timeout(15):
        handle = await start(env, queue)
        assert await handle.result() == "SUCCEEDED"
        history = await handle.fetch_history()
    assert "user-interjection" not in history.to_json()
    assert len(script.turns) == 1 and not script.turns[0].interjectible
    await Replayer(workflows=[CodingTaskWorkflow]).replay_workflow(history)
