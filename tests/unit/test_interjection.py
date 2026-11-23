import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio import workflow
from temporalio.testing import ActivityEnvironment

from sca.agents.codex import CodexBackend
from sca.agents.dsh import DshBackend
from sca.agents.fake import FakeBackend
from sca.agents.opencode import OpenCodeBackend
from sca.agents.pi import PiBackend
from sca.config import Settings
from sca.core.model_provider import GatewayError
from sca.core.models import AgentContext, ExecOutput
from sca.db.repo import Store
from sca.kernel.activities import KernelActivities, TurnInput
from sca.kernel.interjection import SettleTurnSpend, TurnGuidance, with_guidance
from sca.kinds.coding.workflow import CodingTaskWorkflow
from tests.interjection_harness import (
    GUIDANCE_HEADER,
    GatewayInterjectionScript,
    InterjectionScript,
)


def test_guidance_is_prepended_in_delivery_order():
    assert with_guidance("original", []) == "original"
    assert with_guidance("original", ["first\nline", "second"]) == (
        GUIDANCE_HEADER + "\n\nfirst\nline\n\nsecond\n\noriginal"
    )


def make_workflow(monkeypatch, script):
    activities = script.activities()

    async def execute(name, arg=None, *, args=None, **kwargs):
        return await activities[name](*(args if args is not None else [arg]))

    def start(name, arg, *, cancellation_type=None, **kwargs):
        if name != "settle_turn_spend":
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
    guide = script.turns[-1].guidance
    assert guide.message_ids == [first, update]
    assert guide.interrupted_ids == [update]


class SpendStoreFixture(Store):
    def __init__(self, task_limit, daily_limit=None):
        self.row = {
            "id": "task", "tenant_id": "tenant", "kind": "coding", "state": "RUNNING",
            "spec": {"max_cost_usd": task_limit}, "result": {}, "usage": {}, "created_at": datetime.now(UTC),
        }
        self.daily_limit = Decimal(str(daily_limit)) if daily_limit is not None else None
        self.lock = asyncio.Lock()
        self.waiting_for_budget = asyncio.Event()
        self.events = {}
        self.get_task_champion = AsyncMock(return_value=None)
        self.get_session_id = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _transaction(self):
        async with self.lock:
            yield self

    async def _task(self, session, task_id, lock=False):
        assert task_id == self.row["id"]
        return self.row

    async def scalar(self, stmt):
        return self.daily_limit

    async def execute(self, stmt):
        return SimpleNamespace(mappings=lambda: [self.row])

    async def get_task(self, task_id):
        return SimpleNamespace(**self.row)

    async def _write_turn_spend(self, session, task, reservations, **columns):
        task["result"][self._TURN_SPEND] = reservations
        task.update(columns)

    async def _event(self, session, task_id, type, payload, dedup_key=None):
        if dedup_key not in self.events:
            self.events[dedup_key] = (type, payload)
            return self.events[dedup_key]
        return None

    async def append_event(self, task_id, type, payload, dedup_key):
        await self._event(self, task_id, type, payload, dedup_key)

    async def start_guided_turn(self, task_id, payload, dedup_key, *_args, **_kwargs):
        await self.append_event(task_id, "agent.turn_started", payload, dedup_key)

    async def reserve_turn_spend(self, *args):
        budget, waiting = await super().reserve_turn_spend(*args)
        if waiting:
            self.waiting_for_budget.set()
        return budget, waiting


@pytest.mark.parametrize("stall_turn", [True, False], ids=["agent-running", "already-polling"])
async def test_interrupt_restarts_before_old_spend_settles(monkeypatch, tmp_path, stall_turn):
    store = SpendStoreFixture(.4, daily_limit=.18)
    script = GatewayInterjectionScript(tmp_path, store, stall_turn=stall_turn)
    task = make_workflow(monkeypatch, script)
    async with asyncio.timeout(5):
        run = asyncio.create_task(task.run("task"))
        await script.turn_entered.wait()
        if not stall_turn:
            await script.gateway.settlement_started.wait()
        msg = script.add("Take a different approach")
        await task.user_message(msg, "interrupt")
        await asyncio.wait_for(script.second_turn_entered.wait(), 3)
        await script.gateway.settlement_started.wait()
        assert store.row["usage"].get("cost_micro_usd", 0) <= 20_000
        assert not run.done()
        assert [cap for _, cap in script.gateway.issued] == [.12, .06]
        assert script.steps[0] == ("checkpoint", 1)
        script.gateway.release_spend.set()
        assert await run == "SUCCEEDED"
    assert store.row["usage"]["cost_micro_usd"] == 50_000
    assert not store.row["result"][Store._TURN_SPEND]
    assert not script.gateway.keys
    settled = [p for event, p in store.events.values() if event == "agent.usage"]
    assert sorted((p["turn"], p["cost_micro_usd"]) for p in settled) == [(1, 30_000), (2, 20_000)]
    positions = [(event, p["turn"]) for event, p in store.events.values()]
    assert positions.index(("agent.turn_interrupted", 1)) < positions.index(("agent.turn_started", 2))
    assert positions.index(("agent.turn_started", 2)) < positions.index(("agent.usage", 1))
    assert script.applied == [msg]


async def test_interrupt_waits_to_issue_when_the_old_key_holds_the_budget(monkeypatch, tmp_path):
    store = SpendStoreFixture(.12)
    script = GatewayInterjectionScript(tmp_path, store)
    task = make_workflow(monkeypatch, script)
    async with asyncio.timeout(5):
        run = asyncio.create_task(task.run("task"))
        await script.turn_entered.wait()
        await task.user_message(script.add("Continue with changes"), "interrupt")
        await store.waiting_for_budget.wait()
        assert len(script.gateway.issued) == 1
        assert not script.second_turn_entered.is_set()
        script.gateway.release_spend.set()
        assert await run == "SUCCEEDED"
    assert [cap for _, cap in script.gateway.issued] == [.12, .09]
    assert store.row["usage"]["cost_micro_usd"] == 50_000


async def test_workflow_cancellation_publishes_terminal_state_with_settled_usage(monkeypatch, tmp_path):
    store = SpendStoreFixture(.12)
    script = GatewayInterjectionScript(tmp_path, store)
    task = make_workflow(monkeypatch, script)
    async with asyncio.timeout(5):
        run = asyncio.create_task(task.run("task"))
        await script.turn_entered.wait()
        run.cancel()
        await script.gateway.settlement_started.wait()
        assert script.first_turn_stopped.is_set()
        assert script.gateway.blocked
        assert "CANCELLED" not in script.states
        script.gateway.release_spend.set()
        with pytest.raises(asyncio.CancelledError):
            await run
    assert script.states[-1] == "CANCELLED"
    assert store.row["usage"]["cost_micro_usd"] == 30_000


async def test_spend_retry_charges_once_after_key_cleanup_fails(tmp_path):
    store = SpendStoreFixture(.12)
    script = GatewayInterjectionScript(tmp_path, store)
    gateway = script.gateway
    alias = "task-task-turn1-attempt1"
    await store.reserve_turn_spend("task", alias, .12, {"turn": 1})
    await gateway.issue_key(alias, max_budget_usd=.12)
    gateway.release_spend.set()
    gateway.fail_delete = True
    inp = SettleTurnSpend("task", 1)
    with pytest.raises(GatewayError, match="fixture delete failed"):
        await script.kernel.settle_turn_spend(inp)
    assert store.row["usage"]["cost_micro_usd"] == 30_000
    await script.kernel.settle_turn_spend(inp)
    await script.kernel.settle_turn_spend(inp)
    assert store.row["usage"]["cost_micro_usd"] == 30_000
    assert len(store.events) == 1
    assert not gateway.keys


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
    settling_key = asyncio.Event()
    release_key = asyncio.Event()

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
        settling_key.set()
        await release_key.wait()

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
        await asyncio.wait_for(settling_key.wait(), .5)
        assert process_stopped.is_set() and not running.done()
        assert any(call.args[1] == "agent.turn_interrupted" for call in store.append_event.await_args_list)
        release_key.set()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert process_stopped.is_set()
    gateway.revoke_key.assert_awaited_once_with("task-task-turn1-attempt1")


@pytest.mark.parametrize("backend", ["codex", "dsh", "fake", "opencode", "pi"])
async def test_cancelling_a_quiet_turn_immediately_closes_execution_stream(backend, tmp_path):
    started, killed = asyncio.Event(), asyncio.Event()
    version = "2.3.0-sca.0"
    executable = tmp_path / version / "opencode"
    executable.parent.mkdir()
    executable.touch()
    agent = {
        "codex": CodexBackend(tmp_path, None, "fixture"),
        "dsh": DshBackend("fixture"),
        "fake": FakeBackend(),
        "opencode": OpenCodeBackend(tmp_path, version, "fixture"),
        "pi": PiBackend("fixture"),
    }[backend]

    class Sandbox:
        async def exec(self, *args, **kwargs):
            started.set()
            try:
                yield ExecOutput(stream="stdout", text="")
                await asyncio.Future()
            finally:
                killed.set()

    async def run():
        ctx = AgentContext(task_id="task", agent_home_host=str(tmp_path), model_base_url="http://model.invalid/v1",
                           model_api_key="fixture")
        async for _ in agent.run_turn(Sandbox(), "sandbox", ctx, "```sca-fake\nsleep 120\n```",
                                      session_id=None, timeout_s=180):
            pass

    task = asyncio.create_task(run())
    async with asyncio.timeout(1):
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert killed.is_set()


@pytest.mark.parametrize("driver", ["docker", "local"])
async def test_scripted_turn_bypasses_model_key_settlement(driver, tmp_path):
    store = SimpleNamespace(
        get_task_champion=AsyncMock(return_value=None), get_session_id=AsyncMock(return_value=None),
        get_task=AsyncMock(return_value=SimpleNamespace(kind="coding")),
        start_guided_turn=AsyncMock(), append_event=AsyncMock(), set_session=AsyncMock(), add_usage=AsyncMock(),
    )
    gateway = SimpleNamespace(issue_key=AsyncMock(), revoke_key=AsyncMock())
    acts = KernelActivities(
        settings=Settings(_env_file=None, data_dir=tmp_path, sandbox_driver=driver), store=store,
        sandbox=SimpleNamespace(), agent=FakeBackend(), objects=SimpleNamespace(), gateway=gateway,
    )
    acts._ensure_state = AsyncMock(return_value=False)
    acts._spec = AsyncMock(return_value=SimpleNamespace(model=None, max_cost_usd=None, turn_timeout_s=30))
    acts._sandbox_id = AsyncMock(return_value="sandbox")
    acts._before_agent_turn = AsyncMock()
    acts._sync_fake_home = AsyncMock()
    result = await ActivityEnvironment().run(acts.run_agent_turn, TurnInput(
        "task", 2, "fixture", interjectible=True, guidance=TurnGuidance(["guide"], ["guide"]),
    ))
    assert result.final_message == "Fake turn completed."
    start = store.start_guided_turn.await_args.args
    assert start[1]["turn"] == 2 and start[3:] == (["guide"], ["guide"])
    gateway.issue_key.assert_not_awaited()
    gateway.revoke_key.assert_not_awaited()
