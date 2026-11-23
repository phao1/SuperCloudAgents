"""Scripted activities shared by interjection workflow tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from temporalio import activity
from temporalio.testing import ActivityEnvironment

from sca.config import Settings
from sca.core.model_provider import GatewayError
from sca.core.models import AgentContext, AgentEvent, AgentEventType
from sca.kernel.activities import (
    CheckpointResult,
    KernelActivities,
    PublishInput,
    SetStateInput,
    StepInput,
    TurnInput,
    TurnResult,
    VerifyResult,
    WorkspaceInfo,
)
from sca.kernel.interjection import (
    MESSAGES_PENDING,
    ApplyMessages,
    DeliverMessages,
    Guidance,
)
from sca.kinds.coding.kind import workflow_spec

GUIDANCE_HEADER = "User guidance received during the run (highest priority, may change direction):"


class InterjectionScript:
    def __init__(self, *, auto_finish: bool = True, legacy: bool = False, block: bool = False):
        self.auto_finish = auto_finish
        self.legacy = legacy
        self.block = block
        self.release = asyncio.Event()
        self.turn_entered = asyncio.Event()
        self.reviewed: asyncio.Queue[int] = asyncio.Queue()
        self.turns: list[TurnInput] = []
        self.verify_turns: list[int] = []
        self.pending: dict[str, Guidance] = {}
        self.applied: list[str] = []
        self.states: list[str] = []
        self.steps: list[tuple[str, int]] = []
        self.workspace: list[str] = []
        self.publish_turns: list[int] = []
        self.before_finish: str | None = None

    def add(self, text: str) -> str:
        message_id = str(uuid4())
        self.pending[message_id] = Guidance(message_id, text)
        return message_id

    @activity.defn
    async def load_task(self, task_id: str) -> dict:
        return workflow_spec({
            "instruction": "Original task",
            "auto_finish": self.auto_finish,
            "max_fix_attempts": 0,
            "turn_timeout_s": 60,
            "turn_heartbeat_timeout_s": 10,
            **({} if self.legacy else {"_user_interjection": True}),
        })

    @activity.defn
    async def set_state(self, inp: SetStateInput) -> str | None:
        if inp.wait_for_messages:
            if self.before_finish is not None:
                self.add(self.before_finish)
                self.before_finish = None
            if self.pending:
                return MESSAGES_PENDING
        self.states.append(inp.state)
        if inp.state == "AWAITING_REVIEW":
            self.reviewed.put_nowait(len(self.turns))
        return None

    @activity.defn(name="coding.prepare_workspace")
    async def prepare(self, task_id: str) -> WorkspaceInfo:
        return WorkspaceInfo("base", "sca/test")

    @activity.defn
    async def create_sandbox(self, task_id: str) -> str:
        return "sandbox"

    @activity.defn
    async def destroy_sandbox(self, task_id: str) -> None:
        pass

    @activity.defn
    async def release_sandbox(self, inp: StepInput) -> None:
        pass

    @activity.defn
    async def restore_sandbox(self, inp: StepInput) -> None:
        pass

    @activity.defn
    async def check_budget(self, inp: StepInput) -> str | None:
        return None

    @activity.defn(name="coding.initial_prompt")
    async def initial(self, spec: dict) -> str:
        return spec["instruction"]

    @activity.defn(name="coding.feedback_prompt")
    async def feedback(self, text: str, unresolved: list[dict]) -> str:
        return "Review revision " + text

    @activity.defn
    async def run_agent_turn(self, inp: TurnInput) -> TurnResult:
        self.turns.append(inp)
        self.steps.append(("start", inp.turn))
        if inp.turn == 1:
            self.workspace.append("edits from the first turn")
            self.turn_entered.set()
        try:
            while inp.turn == 1 and self.block and not self.release.is_set():
                if activity.in_activity():
                    activity.heartbeat()
                await asyncio.sleep(.01)
            return TurnResult("session-1", "done")
        except asyncio.CancelledError:
            await asyncio.sleep(.01)
            self.steps.append(("cancelled", inp.turn))
            raise

    @activity.defn
    async def deliver_task_messages(self, inp: DeliverMessages) -> list[Guidance]:
        return list(self.pending.values())

    @activity.defn
    async def apply_task_messages(self, inp: ApplyMessages) -> None:
        for message_id in inp.message_ids:
            self.pending.pop(message_id)
            self.applied.append(message_id)

    @activity.defn(name="coding.checkpoint_workspace")
    async def checkpoint(self, inp: StepInput) -> CheckpointResult:
        self.steps.append(("checkpoint", inp.turn))
        assert self.workspace
        return CheckpointResult("checkpoint", True)

    @activity.defn(name="coding.run_verification")
    async def verify(self, inp: StepInput) -> VerifyResult:
        self.verify_turns.append(inp.turn)
        return VerifyResult(True, 0, "")

    @activity.defn(name="coding.publish")
    async def publish(self, inp: PublishInput) -> None:
        self.publish_turns.append(inp.turn)

    def activities(self) -> dict:
        return {activity._Definition.must_from_callable(fn).name: fn for fn in (
            self.load_task, self.set_state, self.prepare, self.create_sandbox, self.destroy_sandbox,
            self.release_sandbox, self.restore_sandbox, self.check_budget, self.initial, self.feedback,
            self.run_agent_turn, self.deliver_task_messages, self.apply_task_messages,
            self.checkpoint, self.verify, self.publish,
        )}


class DelayedSpendGateway:
    sandbox_url = "http://gateway.invalid/v1"

    def __init__(self) -> None:
        self.keys: dict[str, float] = {}
        self.issued: list[tuple[str, float]] = []
        self.blocked: set[str] = set()
        self.settlement_started = asyncio.Event()
        self.release_spend = asyncio.Event()
        self.fail_delete = False

    async def issue_key(self, alias, *, max_budget_usd, **kwargs):
        self.issued.append((alias, max_budget_usd))
        self.keys[alias] = .03 if "-turn1-" in alias else .02
        return "fixture-" + alias

    async def block_key(self, alias, *, issued_key=None):
        assert issued_key is None or issued_key == "fixture-" + alias
        if alias not in self.keys:
            return False
        self.blocked.add(alias)
        return True

    async def read_key_spend(self, alias):
        assert alias in self.blocked
        if "-turn1-" in alias:
            self.settlement_started.set()
            await self.release_spend.wait()
        return self.keys[alias], True

    async def delete_key(self, alias):
        if self.fail_delete:
            self.fail_delete = False
            raise GatewayError("fixture delete failed")
        self.keys.pop(alias, None)


class GatewayInterjectionScript(InterjectionScript):
    def __init__(self, tmp_path, store, *, stall_turn=True):
        super().__init__(block=stall_turn)
        self.gateway = DelayedSpendGateway()
        self.first_turn_stopped = asyncio.Event()
        self.second_turn_entered = asyncio.Event()
        self.kernel = KernelActivities(
            settings=Settings(_env_file=None, data_dir=tmp_path, model_gateway_turn_budget_usd=.12),
            store=store, sandbox=SimpleNamespace(),
            agent=SimpleNamespace(name="dsh", run_turn=self.run_backend),
            objects=SimpleNamespace(), gateway=self.gateway,
        )
        self.kernel._ensure_state = AsyncMock(return_value=False)
        self.kernel._spec = AsyncMock(
            return_value=SimpleNamespace(model="fixture", max_cost_usd=None, turn_timeout_s=30),
        )
        self.kernel._sandbox_id = AsyncMock(return_value="sandbox")
        self.kernel._before_agent_turn = AsyncMock()
        self.kernel._attach_tools = AsyncMock(side_effect=lambda task_id, *_: AgentContext(
            task_id=task_id, agent_home_host=str(tmp_path), model="fixture",
        ))
        self.kernel.transport = SimpleNamespace(pull_agent_home=AsyncMock())
        self.activity_env = ActivityEnvironment()

    @activity.defn
    async def load_task(self, task_id: str) -> dict:
        return {**await super().load_task(task_id), "_deferred_turn_spend": True}

    @activity.defn
    async def run_agent_turn(self, inp: TurnInput) -> TurnResult:
        self.turns.append(inp)
        self.workspace.append("edit")
        if activity.in_activity():
            return await self.kernel.run_agent_turn(inp)
        return await self.activity_env.run(self.kernel.run_agent_turn, inp)

    def activities(self) -> dict:
        return {**super().activities(), "settle_turn_spend": self.kernel.settle_turn_spend}

    async def run_backend(self, sandbox, sandbox_id, ctx, *args, **kwargs):
        if len(self.turns) == 1:
            self.turn_entered.set()
            try:
                if self.block:
                    await self.release.wait()
            finally:
                self.first_turn_stopped.set()
        else:
            old, new = (alias for alias, _ in self.gateway.issued)
            assert old in self.gateway.blocked and new not in self.gateway.blocked
            assert self.first_turn_stopped.is_set()
            assert ctx.model_api_key == "fixture-" + new
            self.second_turn_entered.set()
        yield AgentEvent(type=AgentEventType.TURN_COMPLETED, data={"final_message": "done"})
