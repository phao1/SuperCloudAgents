import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio import workflow
from temporalio.testing import ActivityEnvironment

from sca.agents.codex import CodexBackend
from sca.config import Settings
from sca.core.models import ExecOutput
from sca.kernel.activities import KernelActivities, TurnInput
from sca.kernel.interjection import with_guidance
from sca.kinds.coding.workflow import CodingTaskWorkflow
from tests.interjection_harness import GUIDANCE_HEADER, InterjectionScript


def test_guidance_is_prepended_in_delivery_order():
    assert with_guidance("original", []) == "original"
    assert with_guidance("original", ["first\nline", "second"]) == (
        GUIDANCE_HEADER + "\n\nfirst\nline\n\nsecond\n\noriginal"
    )


def make_workflow(monkeypatch, script):
    activities = script.activities()

    async def execute(name, arg=None, *, args=None, **kwargs):
        return await activities[name](*(args if args is not None else [arg]))

    def start(name, arg, *, cancellation_type, **kwargs):
        assert cancellation_type == workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
        return asyncio.create_task(execute(name, arg, **kwargs))

    async def wait_condition(predicate, **kwargs):
        while not predicate():
            await asyncio.sleep(.001)

    monkeypatch.setattr(workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow, "start_activity", start)
    monkeypatch.setattr(workflow, "patched", lambda patch: not script.legacy)
    monkeypatch.setattr(workflow, "wait_condition", wait_condition)
    return CodingTaskWorkflow()


async def test_queue_order_and_extra_turn_after_verification_passes(monkeypatch):
    script = InterjectionScript(block=True)
    task = make_workflow(monkeypatch, script)
    async with asyncio.timeout(5):
        run = asyncio.create_task(task.run("task"))
        await script.turn_entered.wait()
        first = script.add("first")
        second = script.add("second")
        await task.user_message(second, "queue")
        await task.user_message(first, "queue")
        await task.user_message(first, "queue")
        assert script.steps == [("start", 1)]
        script.release.set()
        assert await run == "SUCCEEDED"

    original, revision = script.turns
    assert original.prompt.strip() == "Original task"
    assert revision.prompt.startswith(GUIDANCE_HEADER + "\n\nfirst\n\nsecond\n\n")
    assert revision.session_id == "session-1"
    assert script.verify_turns == [1, 2]
    assert script.publish_turns == [2]
    assert script.applied == [first, second]


async def test_terminal_check_preserves_a_message_whose_signal_is_late(monkeypatch):
    script = InterjectionScript()
    script.before_finish = "Accepted while finalizing"
    task = make_workflow(monkeypatch, script)
    assert await asyncio.wait_for(task.run("task"), 5) == "SUCCEEDED"
    assert len(script.turns) == 2
    assert script.turns[-1].prompt.startswith(GUIDANCE_HEADER + "\n\nAccepted while finalizing")
    assert len(script.applied) == 1


async def test_interrupt_waits_for_cleanup_then_checkpoints_and_restarts(monkeypatch):
    script = InterjectionScript(block=True)
    first = script.add("original guidance")
    task = make_workflow(monkeypatch, script)
    async with asyncio.timeout(5):
        run = asyncio.create_task(task.run("task"))
        await script.turn_entered.wait()
        update = script.add("change direction")
        await task.user_message(update, "interrupt")
        assert await run == "SUCCEEDED"

    assert script.steps == [
        ("start", 1), ("cancelled", 1), ("checkpoint", 1), ("start", 2), ("checkpoint", 2),
    ]
    assert script.verify_turns == [2]
    assert script.turns[-1].prompt.startswith(
        GUIDANCE_HEADER + "\n\noriginal guidance\n\nchange direction\n\n",
    )
    assert script.applied == [first, update]


async def test_guidance_during_review_requests_revision(monkeypatch):
    script = InterjectionScript(auto_finish=False)
    task = make_workflow(monkeypatch, script)
    async with asyncio.timeout(5):
        run = asyncio.create_task(task.run("task"))
        assert await script.reviewed.get() == 1
        message = script.add("Add empty-input coverage")
        await task.user_message(message, "interrupt")
        assert await script.reviewed.get() == 2
        await task.submit_review("approve")
        assert await run == "SUCCEEDED"
    assert script.turns[-1].prompt.startswith(GUIDANCE_HEADER + "\n\nAdd empty-input coverage")
    assert script.publish_turns == [1, 2]
    assert script.applied == [message]


async def test_cancel_while_saving_an_agent_event_awaits_process_cleanup(tmp_path):
    writing_session = asyncio.Event()
    process_stopped = asyncio.Event()

    class Sandbox:
        async def exec(self, *args, **kwargs):
            try:
                yield ExecOutput(stream="stdout", text='{"type":"thread.started","thread_id":"session"}')
            finally:
                await asyncio.sleep(.01)
                process_stopped.set()

    async def save_session(*args):
        writing_session.set()
        await asyncio.Future()

    async def revoke_key(alias):
        assert process_stopped.is_set()

    store = SimpleNamespace(
        get_task_champion=AsyncMock(return_value=None), get_session_id=AsyncMock(return_value=None),
        get_task=AsyncMock(return_value=SimpleNamespace(kind="coding")),
        append_event=AsyncMock(), set_session=save_session,
    )
    gateway = SimpleNamespace(
        sandbox_url="http://gateway.invalid/v1", issue_key=AsyncMock(return_value="fixture"),
        revoke_key=AsyncMock(side_effect=revoke_key),
    )
    acts = KernelActivities(
        settings=Settings(_env_file=None, data_dir=tmp_path), store=store, sandbox=Sandbox(),
        agent=CodexBackend(tmp_path, None, "fixture"), objects=SimpleNamespace(), gateway=gateway,
    )
    acts._ensure_state = AsyncMock(return_value=False)
    acts._spec = AsyncMock(return_value=SimpleNamespace(model=None, max_cost_usd=None, turn_timeout_s=30))
    acts._sandbox_id = AsyncMock(return_value="sandbox")
    acts._before_agent_turn = AsyncMock()
    env = ActivityEnvironment()
    async with asyncio.timeout(5):
        running = asyncio.create_task(env.run(acts.run_agent_turn, TurnInput("task", 1, "work", interjectible=True)))
        await writing_session.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert process_stopped.is_set()
    gateway.revoke_key.assert_awaited_once_with("task-task-turn1-attempt1")
