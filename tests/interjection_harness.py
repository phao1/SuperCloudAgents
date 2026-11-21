"""Scripted activities shared by interjection workflow tests."""

import asyncio
from uuid import uuid4

from temporalio import activity

from sca.kernel.activities import (
    CheckpointResult,
    PublishInput,
    SetStateInput,
    StepInput,
    TurnInput,
    TurnResult,
    VerifyResult,
    WorkspaceInfo,
)
from sca.kernel.interjection import MESSAGES_PENDING, ApplyMessages, DeliverMessages, Guidance
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
