"""Branches of the coding workflow, with fake activities on a real Temporal server."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Self

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from sca.config import get_settings
from sca.core.models import TaskState
from sca.db.repo import Store
from sca.kinds.coding.activities import (
    CheckpointResult,
    CiOutcome,
    CiRequest,
    PublishInput,
    SetStateInput,
    StepInput,
    TurnInput,
    TurnResult,
    VerifyResult,
    WorkspaceInfo,
)
from sca.kinds.coding.kind import prompt_activities, workflow_spec
from sca.kinds.coding.prompts import planning_prompt
from sca.kinds.coding.spec import CodingSpec
from sca.kinds.coding.subtask_inputs import MergeInput, PlanningInput, SubtaskInput
from sca.kinds.coding.workflow import (
    REVIEW_SIGNAL,
    CodingAfterVerify,
    CodingBeforeTurn,
    CodingSubtaskWorkflow,
    CodingTaskWorkflow,
)

pytestmark = pytest.mark.integration


@dataclass
class Fakes:
    """Scripted activities that record what the workflow asked for."""

    admission_store: Store | None = None
    admission_blocked: bool = False
    admission_attempts: int = 0
    admission_held: bool = False
    plan: str = "none"
    plan_review: bool = False
    plans: list = field(default_factory=list)
    planning_feedback: list = field(default_factory=list)
    planning_prompts: list[str] = field(default_factory=list)
    plan_rejections: list = field(default_factory=list)
    subtasks: list[dict] = field(default_factory=list)
    child_started: list[str] = field(default_factory=list)
    child_finished: list[str] = field(default_factory=list)
    child_destroyed: list[str] = field(default_factory=list)
    child_blocks: bool = False
    conflict: bool = False
    child_fails: bool = False
    child_crashes: bool = False
    child_results: list = field(default_factory=list)
    verify_results: list[bool] = field(default_factory=lambda: [True])
    max_fix_attempts: int = 2
    fail_prepare: bool = False
    turn_blocks: bool = False
    turn_fails: bool = False
    budget_checks: int = 0
    over_budget_check: int | None = None
    review_idle_release_s: float = 600
    released: int = 0
    ci: dict | None = None
    over_budget_after_turn: int | None = None
    ci_recorded: list = field(default_factory=list)
    commits: int = 0
    restored: int = 0
    states: list[str] = field(default_factory=list)
    reasons: list[str | None] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    sessions: list[str | None] = field(default_factory=list)
    published: list[bool] = field(default_factory=list)
    destroyed: int = 0

    def all(self) -> list:
        fakes = self

        @activity.defn(name="acquire_slot")
        async def acquire_slot(task_id: str) -> dict:
            fakes.admission_attempts += 1
            if fakes.admission_store is not None:
                return await fakes.admission_store.acquire_slot(task_id, 4)
            if fakes.admission_blocked:
                return {"granted": False}
            fakes.admission_held = True
            return {"granted": True}

        @activity.defn(name="release_slot")
        async def release_slot(task_id: str) -> None:
            fakes.admission_held = False
            if fakes.admission_store is not None:
                await fakes.admission_store.release_slot(task_id)

        @activity.defn(name="load_task")
        async def load_task(task_id: str) -> dict:
            spec = CodingSpec(
                plan=fakes.plan,
                plan_review=fakes.plan_review,
                subtasks=fakes.subtasks,
                repo_url="x",
                instruction="do it",
                verify_command="pytest",
                max_fix_attempts=fakes.max_fix_attempts,
            ).model_dump()
            return workflow_spec(
                {**spec, "_admission": True, "ci": fakes.ci, "review_idle_release_s": fakes.review_idle_release_s}
            )

        @activity.defn(name="set_state")
        async def set_state(inp: SetStateInput) -> None:
            if inp.state in {"AWAITING_REVIEW", "PAUSED", "SUCCEEDED", "FAILED", "CANCELLED"}:
                fakes.admission_held = False
            if fakes.admission_store is not None:
                await fakes.admission_store.set_state(inp.task_id, TaskState(inp.state))
            fakes.states.append(inp.state)
            fakes.reasons.append(inp.reason)

        @activity.defn(name="coding.prepare_workspace")
        async def prepare_workspace(task_id: str) -> WorkspaceInfo:
            if fakes.fail_prepare:
                raise ApplicationError("no such repo", type="InvalidTaskError", non_retryable=True)
            return WorkspaceInfo(base_sha="abc", branch="sca/x")

        @activity.defn(name="create_sandbox")
        async def create_sandbox(task_id: str) -> str:
            return "sbx"

        @activity.defn(name="run_agent_turn")
        async def run_agent_turn(inp: TurnInput) -> TurnResult:
            fakes.prompts.append(inp.prompt)
            fakes.sessions.append(inp.session_id)
            if fakes.turn_fails:
                raise ApplicationError("gateway refused", non_retryable=True)
            while fakes.turn_blocks:
                activity.heartbeat()
                await asyncio.sleep(0.2)
            return TurnResult(session_id="sess-1", final_message="done")

        @activity.defn(name="coding.checkpoint_workspace")
        async def checkpoint_workspace(inp: StepInput) -> CheckpointResult:
            return CheckpointResult(commit="c", changed=True)

        @activity.defn(name="coding.run_verification")
        async def run_verification(inp: StepInput) -> VerifyResult:
            passed = fakes.verify_results.pop(0) if fakes.verify_results else True
            return VerifyResult(passed=passed, exit_code=0 if passed else 1, output_tail="E boom")

        @activity.defn(name="coding.publish")
        async def publish(inp: PublishInput) -> None:
            fakes.published.append(inp.verification_passed)

        @activity.defn(name="check_budget")
        async def check_budget(inp: StepInput) -> str | None:
            fakes.budget_checks += 1
            if fakes.over_budget_check == fakes.budget_checks:
                return "cost budget exhausted"
            if fakes.over_budget_after_turn and len(fakes.prompts) >= fakes.over_budget_after_turn:
                return "task used 11 tokens of 10"
            return None

        @activity.defn(name="coding.request_ci")
        async def request_ci(inp: StepInput) -> CiRequest:
            fakes.commits += 1
            return CiRequest(commit=f"c{fakes.commits}", branch="sca/x")

        @activity.defn(name="coding.record_ci")
        async def record_ci(outcome: CiOutcome) -> None:
            fakes.ci_recorded.append((outcome.commit, outcome.passed, outcome.timed_out))

        @activity.defn(name="release_sandbox")
        async def release_sandbox(inp: StepInput) -> None:
            fakes.released += 1

        @activity.defn(name="restore_sandbox")
        async def restore_sandbox(inp: StepInput) -> None:
            fakes.restored += 1

        @activity.defn(name="destroy_sandbox")
        async def destroy_sandbox(task_id: str) -> None:
            fakes.destroyed += 1

        @activity.defn(name="coding.prepare_mirror")
        async def prepare_mirror(task_id: str) -> WorkspaceInfo:
            return WorkspaceInfo("base", "sca/x")

        @activity.defn(name="coding.build_merged_workspace")
        async def build_merged_workspace(task_id: str) -> None:
            pass

        @activity.defn(name="coding.subtask_step")
        async def subtask_step(inp: SubtaskInput) -> dict:
            if inp.action == "run_agent_turn":
                fakes.child_started.append(inp.name)
                if fakes.child_crashes:
                    raise ApplicationError("child crashed", non_retryable=True)
                async with asyncio.timeout(10):
                    while len(fakes.child_started) < 2 or fakes.child_blocks:
                        activity.heartbeat()
                        await asyncio.sleep(0.05)
                # Both child turns started before either completed.
                assert len(fakes.child_started) == 2
                fakes.child_finished.append(inp.name)
                return {"session_id": inp.name, "final_message": "done"}
            if inp.action == "destroy_sandbox":
                fakes.child_destroyed.append(inp.name)
            if inp.action == "checkpoint_workspace":
                return {"commit": inp.name, "changed": True}
            if inp.action == "run_verification":
                return {"passed": not fakes.child_fails, "exit_code": 1, "output_tail": "bad"}
            return {}

        @activity.defn(name="coding.merge_subtasks")
        async def merge_subtasks(inp: MergeInput) -> list[dict]:
            fakes.child_results = inp.results
            return [
                {
                    "name": r.name,
                    "status": (
                        "failed"
                        if not r.passed
                        else "conflict"
                        if fakes.conflict and i
                        else "merged"
                    ),
                }
                for i, r in enumerate(inp.results)
            ]

        @activity.defn(name="coding.plan_task")
        async def plan_task(inp: PlanningInput) -> dict | None:
            fakes.planning_feedback.append(inp.feedback)
            fakes.planning_prompts.append(planning_prompt("do it", inp.feedback))
            result = fakes.plans.pop(0)
            if isinstance(result, str):
                fakes.plan_rejections.append(result)
                return None
            return result

        @activity.defn(name="coding.reject_plan")
        async def reject_plan(inp: PlanningInput) -> None:
            fakes.plan_rejections.append(inp.feedback)

        return [
            acquire_slot,
            release_slot,
            plan_task,
            reject_plan,
            prepare_mirror,
            build_merged_workspace,
            subtask_step,
            merge_subtasks,
            load_task,
            set_state,
            prepare_workspace,
            create_sandbox,
            run_agent_turn,
            checkpoint_workspace,
            run_verification,
            publish,
            destroy_sandbox,
            release_sandbox,
            restore_sandbox,
            check_budget,
            request_ci,
            record_ci,
        ]


class Harness:
    def __init__(self, client: Client, fakes: Fakes) -> None:
        self.client, self.fakes = client, fakes
        self.queue = f"test-{uuid.uuid4()}"
        self.handle = None

    async def __aenter__(self) -> Self:
        self.worker = Worker(
            self.client,
            task_queue=self.queue,
            workflows=[
                CodingTaskWorkflow,
                CodingSubtaskWorkflow,
                CodingBeforeTurn,
                CodingAfterVerify,
            ],
            activities=self.fakes.all() + prompt_activities(),
        )
        self._task = asyncio.create_task(self.worker.run())
        task_id = str(uuid.uuid4())
        if self.fakes.admission_store is not None:
            task, _ = await self.fakes.admission_store.create_task(
                tenant_id="workflow", kind="coding", spec={}, idempotency_key=None)
            task_id = str(task.id)
        self.task_id = task_id
        self.handle = await self.client.start_workflow(
            CodingTaskWorkflow.run, task_id, id=f"test-{task_id}", task_queue=self.queue
        )
        return self

    async def __aexit__(self, *exc) -> None:
        await self.worker.shutdown()
        await self._task

    async def wait_state(self, state: str, count: int = 1) -> None:
        async with asyncio.timeout(20):
            while self.fakes.states.count(state) < count:
                await asyncio.sleep(0.05)

    async def review(self, decision: str, feedback: str | None = None) -> None:
        await self.handle.signal(REVIEW_SIGNAL, args=[decision, feedback])


@pytest.fixture
async def client() -> Client:
    s = get_settings()
    return await Client.connect(s.temporal_address, namespace=s.temporal_namespace)


async def test_passes_first_time_and_is_approved(client):
    fakes = Fakes()
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        await h.review("approve")
        assert await h.handle.result() == "SUCCEEDED"
    assert fakes.states == ["PROVISIONING", "RUNNING", "VERIFYING", "AWAITING_REVIEW", "SUCCEEDED"]
    assert fakes.published == [True]
    assert fakes.destroyed == 1
    assert "do it" in fakes.prompts[0] and "pytest" in fakes.prompts[0]


async def test_fix_loop_then_pass(client):
    fakes = Fakes(verify_results=[False, True])
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        await h.review("approve")
        await h.handle.result()
    assert len(fakes.prompts) == 2
    assert "E boom" in fakes.prompts[1]
    assert fakes.sessions == [None, "sess-1"]  # the fix turn resumes the session
    assert fakes.published == [True]


async def test_fix_attempts_exhausted_goes_to_review_as_failed(client):
    fakes = Fakes(verify_results=[False, False, False, False], max_fix_attempts=2)
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        await h.review("reject", "not good enough")
        assert await h.handle.result() == "CANCELLED"
    assert len(fakes.prompts) == 3  # first turn + 2 fixes
    assert fakes.published == [False]
    assert fakes.states[-1] == "CANCELLED" and fakes.reasons[-1] == "not good enough"
    assert fakes.destroyed == 1


async def test_request_changes_runs_another_turn(client):
    fakes = Fakes()
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        await h.review("request_changes", "rename the function")
        await h.wait_state("AWAITING_REVIEW", count=2)
        await h.review("approve")
        assert await h.handle.result() == "SUCCEEDED"
    assert len(fakes.prompts) == 2 and "rename the function" in fakes.prompts[1]
    assert fakes.published == [True, True]


async def test_non_retryable_activity_failure_fails_the_task(client):
    fakes = Fakes(fail_prepare=True)
    async with Harness(client, fakes) as h:
        with pytest.raises(WorkflowFailureError):
            await h.handle.result()
    assert fakes.states == ["PROVISIONING", "FAILED"]
    assert "no such repo" in (fakes.reasons[-1] or "")
    assert fakes.destroyed == 1


async def test_cancel_during_agent_turn(client):
    fakes = Fakes(turn_blocks=True)
    async with Harness(client, fakes) as h:
        await h.wait_state("RUNNING")
        await asyncio.sleep(0.5)
        await h.handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await h.handle.result()
        fakes.turn_blocks = False
    assert fakes.states[-1] == "CANCELLED"
    assert fakes.destroyed == 1


async def test_cancel_while_awaiting_review(client):
    fakes = Fakes()
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        await h.handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await h.handle.result()
    assert fakes.states[-1] == "CANCELLED"
    assert fakes.destroyed == 1


async def test_idle_review_releases_the_sandbox_and_more_work_restores_it(client):
    fakes = Fakes(review_idle_release_s=1)
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        async with asyncio.timeout(20):
            while not fakes.released:
                await asyncio.sleep(0.05)
        assert fakes.restored == 0
        await h.review("request_changes", "more")
        await h.wait_state("AWAITING_REVIEW", count=2)
        assert fakes.restored == 1
        await h.review("approve")  # arrives before the idle timer: nothing to release
        assert await h.handle.result() == "SUCCEEDED"
    assert fakes.released == 1 and fakes.restored == 1
    assert fakes.destroyed == 1


async def test_prompt_review_keeps_the_sandbox(client):
    fakes = Fakes()
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        await h.review("request_changes", "more")
        await h.wait_state("AWAITING_REVIEW", count=2)
        await h.review("approve")
        await h.handle.result()
    assert fakes.released == 0 and fakes.restored == 0


async def test_idle_review_releases_slot_and_feedback_reacquires(client):
    fakes = Fakes(review_idle_release_s=1)
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        async with asyncio.timeout(20):
            while not fakes.released:
                await asyncio.sleep(0.05)
        await h.review("request_changes", "more")
        await h.wait_state("AWAITING_REVIEW", count=2)
        await h.review("approve")
        await h.handle.result()
    assert fakes.released == 1 and fakes.restored == 1
    assert fakes.admission_attempts >= 3
    assert not fakes.admission_held


async def test_pause_takes_effect_between_turns_and_resume_continues(client):
    fakes = Fakes(verify_results=[False, True], turn_blocks=True)
    async with Harness(client, fakes) as h:
        await h.wait_state("RUNNING")
        await h.handle.signal("pause")
        await asyncio.sleep(0.5)
        assert "PAUSED" not in fakes.states  # the running turn is not interrupted
        fakes.turn_blocks = False
        await h.wait_state("PAUSED")
        assert fakes.released == 1 and len(fakes.prompts) == 1
        await asyncio.sleep(0.5)
        assert len(fakes.prompts) == 1  # nothing runs while paused
        await h.handle.signal("resume")
        await h.wait_state("AWAITING_REVIEW")
        assert fakes.restored == 1 and len(fakes.prompts) == 2
        await h.review("approve")
        assert await h.handle.result() == "SUCCEEDED"
    assert fakes.states == [
        "PROVISIONING",
        "RUNNING",
        "VERIFYING",
        "PAUSED",
        "RUNNING",
        "VERIFYING",
        "AWAITING_REVIEW",
        "SUCCEEDED",
    ]


async def test_cancel_while_paused(client):
    fakes = Fakes(verify_results=[False], turn_blocks=True)
    async with Harness(client, fakes) as h:
        await h.wait_state("RUNNING")
        await h.handle.signal("pause")
        fakes.turn_blocks = False
        await h.wait_state("PAUSED")
        await h.handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await h.handle.result()
    assert fakes.states[-1] == "CANCELLED"
    assert fakes.destroyed == 1


async def _wait(predicate) -> None:
    async with asyncio.timeout(20):
        while not predicate():
            await asyncio.sleep(0.05)


async def test_ci_failure_is_fixed_and_a_stale_report_is_ignored(client):
    fakes = Fakes(ci={"timeout_s": 60})
    async with Harness(client, fakes) as h:
        await _wait(lambda: fakes.commits == 1)
        assert "AWAITING_REVIEW" not in fakes.states  # review waits for CI
        await h.handle.signal("ci_result", args=["c1", False, "lint: unused import", "http://ci/1"])
        await _wait(lambda: fakes.commits == 2)
        assert "lint: unused import" in fakes.prompts[1] and "http://ci/1" in fakes.prompts[1]
        # A repeated report about the old commit says nothing about the new one.
        await h.handle.signal("ci_result", args=["c1", True, None, None])
        await asyncio.sleep(0.5)
        assert "AWAITING_REVIEW" not in fakes.states
        await h.handle.signal("ci_result", args=["c2", True, None, None])
        await h.wait_state("AWAITING_REVIEW")
        await h.review("approve")
        assert await h.handle.result() == "SUCCEEDED"
    assert fakes.ci_recorded == [("c1", False, False), ("c2", True, False)]
    assert fakes.published == [True]


async def test_ci_silence_counts_as_failure(client):
    fakes = Fakes(ci={"timeout_s": 1}, max_fix_attempts=0)
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        await h.review("reject")
        await h.handle.result()
    assert fakes.ci_recorded == [("c1", False, True)]
    assert fakes.published == [False]


async def test_local_failure_does_not_reach_ci(client):
    fakes = Fakes(ci={"timeout_s": 60}, verify_results=[False], max_fix_attempts=0)
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        await h.review("reject")
        await h.handle.result()
    assert fakes.commits == 0 and fakes.published == [False]


async def test_over_budget_task_is_stopped(client):
    fakes = Fakes(verify_results=[False, False, False], over_budget_after_turn=2)
    async with Harness(client, fakes) as h:
        with pytest.raises(WorkflowFailureError):
            await h.handle.result()
    assert len(fakes.prompts) == 2  # no third turn
    assert fakes.states[-1] == "FAILED" and "budget exceeded" in fakes.reasons[-1]
    assert fakes.destroyed == 1


SUBTASKS = [{"name": n, "instruction": f"do {n}"} for n in ["one", "two"]]


async def test_subtasks_overlap_and_verify_without_parent_turn(client):
    fakes = Fakes(subtasks=SUBTASKS)
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        assert len(fakes.child_finished) == 2 and not fakes.prompts
        assert fakes.published == [True]
        assert sorted(fakes.child_destroyed) == ["one", "two"]
        await h.review("approve")
        assert await h.handle.result() == "SUCCEEDED"


@pytest.mark.parametrize("failed", [False, True])
async def test_unmerged_subtasks_review_and_feedback(client, failed):
    fakes = Fakes(subtasks=SUBTASKS, conflict=not failed, child_fails=failed, max_fix_attempts=0)
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        assert fakes.published == [False] and not fakes.prompts
        await h.review("request_changes", "resolve it")
        await h.wait_state("AWAITING_REVIEW", 2)
        assert "two" in fakes.prompts[0] and "/workspace/.sca/subtask-two.patch" in fakes.prompts[0]
        assert fakes.published == [False, True]
        await h.review("approve")
        await h.handle.result()


async def test_cancel_parent_waits_for_child_cleanup(client):
    fakes = Fakes(subtasks=SUBTASKS, child_blocks=True)
    async with Harness(client, fakes) as h:
        await _wait(lambda: len(fakes.child_started) == 2)
        await h.handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await h.handle.result()
        assert sorted(fakes.child_destroyed) == ["one", "two"]
    assert fakes.states[-1] == "CANCELLED" and fakes.destroyed == 1


async def test_crashed_children_are_reported_for_review(client):
    fakes = Fakes(subtasks=SUBTASKS, child_crashes=True)
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        assert fakes.published == [False]
        assert all(not r.passed and "child crashed" in r.reason for r in fakes.child_results)
        assert sorted(fakes.child_destroyed) == ["one", "two"]
        await h.review("approve")
        assert await h.handle.result() == "SUCCEEDED"


async def test_budget_checked_before_second_turn(client):
    fakes = Fakes(verify_results=[False], over_budget_check=3)
    async with Harness(client, fakes) as h:
        with pytest.raises(WorkflowFailureError):
            await h.handle.result()
    assert len(fakes.prompts) == 1
    assert fakes.states[-1] == "FAILED"
    assert "budget" in fakes.reasons[-1]


async def test_failed_turn_over_budget_is_not_retried(client):
    fakes = Fakes(turn_fails=True, over_budget_check=2)
    async with Harness(client, fakes) as h:
        with pytest.raises(WorkflowFailureError):
            await h.handle.result()
    assert len(fakes.prompts) == 1
    assert fakes.states[-1] == "FAILED"
    assert "cost budget exhausted" in fakes.reasons[-1]


@pytest.mark.parametrize("outcome", ["accepted", "no_split", "overlap"])
async def test_automatic_planning_routes(client, outcome):
    plan = {"split": True, "subtasks": SUBTASKS, "reason": "independent"}
    result = (
        plan
        if outcome == "accepted"
        else (
            {"split": False, "reason": "coupled", "subtasks": []}
            if outcome == "no_split"
            else "overlapping paths: calc.py"
        )
    )
    fakes = Fakes(plan="auto", plans=[result])
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        assert bool(fakes.child_started) == (outcome == "accepted")
        assert bool(fakes.prompts) == (outcome != "accepted")
        assert bool(fakes.plan_rejections) == (outcome == "overlap")
        await h.review("approve")
        assert await h.handle.result() == "SUCCEEDED"


@pytest.mark.parametrize("decision", ["approve", "request_changes", "reject"])
async def test_automatic_plan_review(client, decision):
    plan = {"split": True, "subtasks": SUBTASKS, "reason": "independent"}
    fakes = Fakes(plan="auto", plan_review=True, plans=[plan, plan])
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        assert not fakes.child_started and not fakes.prompts
        await h.review(decision, "separate the tests")
        if decision == "reject":
            assert await h.handle.result() == "CANCELLED"
            assert not fakes.child_started
            return
        if decision == "request_changes":
            await h.wait_state("AWAITING_REVIEW", 2)
            assert fakes.planning_feedback == [None, "separate the tests"]
            assert "separate the tests" in fakes.planning_prompts[1]
            await h.review("approve")
        await h.wait_state("AWAITING_REVIEW", 3 if decision == "request_changes" else 2)
        assert sorted(fakes.child_started) == ["one", "two"]
        await h.review("approve")
        assert await h.handle.result() == "SUCCEEDED"


async def test_cancel_during_admission_wait(client):
    fakes = Fakes(admission_blocked=True)
    async with Harness(client, fakes) as h:
        async with asyncio.timeout(20):
            while not fakes.admission_attempts:
                await asyncio.sleep(0.05)
        assert not fakes.states and not fakes.prompts
        await h.handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await h.handle.result()
    assert fakes.states == ["CANCELLED"]
    assert not fakes.admission_held


async def test_postgres_review_releases_and_feedback_waits_for_slot(client, store):
    await store.set_tenant_quota("workflow", 1)
    fakes = Fakes(admission_store=store, review_idle_release_s=1)
    async with Harness(client, fakes) as h:
        await h.wait_state("AWAITING_REVIEW")
        async with asyncio.timeout(20):
            while not fakes.released:
                await asyncio.sleep(0.05)
        competitor, _ = await store.create_task(
            tenant_id="workflow", kind="coding", spec={}, idempotency_key=None)
        assert (await store.acquire_slot(str(competitor.id), 4))["granted"]
        attempts = fakes.admission_attempts
        await h.review("request_changes", "more")
        async with asyncio.timeout(20):
            while fakes.admission_attempts == attempts:
                await asyncio.sleep(0.05)
        assert fakes.restored == 0
        await store.set_state(competitor.id, TaskState.CANCELLED)
        await h.wait_state("AWAITING_REVIEW", count=2)
        assert fakes.restored == 1
        await h.review("approve")
        assert await h.handle.result() == "SUCCEEDED"
    assert (await store.get_task(h.task_id)).state == TaskState.SUCCEEDED


async def test_postgres_cancel_while_waiting(client, store):
    await store.set_tenant_quota("workflow", 0)
    fakes = Fakes(admission_store=store)
    async with Harness(client, fakes) as h:
        async with asyncio.timeout(20):
            while not fakes.admission_attempts:
                await asyncio.sleep(0.05)
        await h.handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await h.handle.result()
    assert (await store.get_task(h.task_id)).state == TaskState.CANCELLED
    assert not fakes.prompts
