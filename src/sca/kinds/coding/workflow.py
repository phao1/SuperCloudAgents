"""The coding task workflow: the order of the steps (design.md §7.2).

No I/O here. Everything that touches the outside world is an activity.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    ChildWorkflowError,
    is_cancelled_exception,
)

with workflow.unsafe.imports_passed_through():
    from sca.kinds.coding import prompts
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
    from sca.kinds.coding.subtask_inputs import MergeInput, SubtaskInput
    from sca.workspace.merge import SubtaskResult

WORKFLOW_NAME = "CodingTaskWorkflow"
REVIEW_SIGNAL = "submit_review"

_QUICK = {"start_to_close_timeout": timedelta(seconds=30)}
_NO_RETRY = RetryPolicy(maximum_attempts=1)


def _retry(attempts: int) -> RetryPolicy:
    return RetryPolicy(maximum_attempts=attempts, initial_interval=timedelta(seconds=2))


@workflow.defn(name=WORKFLOW_NAME)
class CodingTaskWorkflow:
    def __init__(self) -> None:
        self._reviews: list[tuple[str, str | None]] = []
        self._pause_requested = False
        self._sandbox_up = False
        self._ci_results: dict[str, tuple[bool, str | None, str | None]] = {}

    @workflow.signal(name="ci_result")
    def ci_result(
        self, commit: str, passed: bool, summary: str | None = None, url: str | None = None
    ) -> None:
        # Keyed by commit: a late report about an older commit cannot pass a newer one.
        self._ci_results[commit] = (passed, summary, url)

    @workflow.signal(name="pause")
    def pause(self) -> None:
        self._pause_requested = True

    @workflow.signal(name="resume")
    def resume(self) -> None:
        self._pause_requested = False

    @workflow.signal(name=REVIEW_SIGNAL)
    def submit_review(self, decision: str, feedback: str | None = None) -> None:
        self._reviews.append((decision, feedback))

    async def _set_state(self, task_id: str, state: str, reason: str | None = None) -> None:
        await workflow.execute_activity(
            "set_state", SetStateInput(task_id, state, reason), **_QUICK
        )

    @workflow.run
    async def run(self, task_id: str) -> str:
        try:
            return await self._run(task_id)
        except BaseException as e:
            if is_cancelled_exception(e):
                await self._finish(task_id, "CANCELLED", "cancelled by user")
            elif isinstance(e, ActivityError):
                cause = e.cause
                reason = f"{e.activity_type}: {getattr(cause, 'message', None) or cause}"
                await self._finish(task_id, "FAILED", reason)
                raise ApplicationError(reason, type="TaskFailed", non_retryable=True) from e
            raise
        finally:
            await self._destroy_sandbox(task_id)

    async def _finish(self, task_id: str, state: str, reason: str) -> None:
        try:
            await self._set_state(task_id, state, reason)
        except ActivityError:
            workflow.logger.exception("could not record final state %s", state)

    async def _destroy_sandbox(self, task_id: str) -> None:
        try:
            await workflow.execute_activity(
                "destroy_sandbox",
                task_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_retry(5),
            )
        except ActivityError:
            workflow.logger.exception("could not destroy sandbox")

    async def _release_sandbox(self, task_id: str, turn: int) -> None:
        if self._sandbox_up:
            await workflow.execute_activity(
                "release_sandbox",
                StepInput(task_id, turn),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_retry(5),
            )
            self._sandbox_up = False

    async def _restore_sandbox(self, task_id: str, turn: int) -> None:
        if not self._sandbox_up:
            await workflow.execute_activity(
                "restore_sandbox",
                StepInput(task_id, turn),
                start_to_close_timeout=timedelta(minutes=20),
                heartbeat_timeout=timedelta(minutes=5),
                retry_policy=_retry(3),
            )
            self._sandbox_up = True

    async def _run_ci(self, task_id: str, turn: int, ci: dict) -> CiOutcome:
        request: CiRequest = await workflow.execute_activity(
            "request_ci",
            StepInput(task_id, turn),
            result_type=CiRequest,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_retry(3),
        )
        try:
            await workflow.wait_condition(
                lambda: request.commit in self._ci_results,
                timeout=timedelta(seconds=ci.get("timeout_s", 3600)),
            )
            ok, summary, url = self._ci_results[request.commit]
            outcome = CiOutcome(task_id, turn, request.commit, ok, summary, url)
        except TimeoutError:
            outcome = CiOutcome(
                task_id,
                turn,
                request.commit,
                False,
                "CI did not report a result in time.",
                None,
                True,
            )
        await workflow.execute_activity("record_ci", outcome, **_QUICK)
        return outcome

    async def _run(self, task_id: str) -> str:
        spec: dict = await workflow.execute_activity("load_task", task_id, **_QUICK)
        checks: list[str] = [
            c for c in [spec.get("verify_command"), *spec.get("verify_commands", [])] if c
        ]
        ci: dict | None = spec.get("ci")
        max_fixes: int = spec["max_fix_attempts"]
        turn_timeout = timedelta(seconds=spec["turn_timeout_s"] + 60)

        await self._set_state(task_id, "PROVISIONING")
        subtasks = spec.get("subtasks", [])
        statuses = []
        if subtasks:
            await workflow.execute_activity(
                "prepare_mirror",
                task_id,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=_retry(3),
            )
            await self._set_state(task_id, "RUNNING")
            handles = [
                await workflow.start_child_workflow(
                    CodingSubtaskWorkflow.run,
                    args=[task_id, sub, spec],
                    id=f"task-{task_id}-sub-{sub['name']}",
                    cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
                for sub in subtasks
            ]

            async def collect(handle, subtask):
                try:
                    return await handle
                except ChildWorkflowError as exc:
                    if is_cancelled_exception(exc):
                        raise
                    return SubtaskResult(
                        subtask["name"],
                        branch=f"sca/{task_id[:8]}-{subtask['name']}",
                        reason=str(exc.cause),
                    )

            results = await asyncio.gather(
                *(
                    collect(handle, subtask)
                    for handle, subtask in zip(handles, subtasks, strict=True)
                )
            )
            statuses = await workflow.execute_activity(
                "merge_subtasks",
                MergeInput(task_id, results),
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=_retry(3),
            )
            await workflow.execute_activity(
                "build_merged_workspace",
                task_id,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=_retry(3),
            )
        else:
            await workflow.execute_activity(
                "prepare_workspace",
                task_id,
                result_type=WorkspaceInfo,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=_retry(3),
            )
        await workflow.execute_activity(
            "create_sandbox",
            task_id,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_retry(3),
        )
        if spec.get("setup_command"):
            await workflow.execute_activity(
                "run_setup",
                task_id,
                start_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(minutes=5),
                retry_policy=_NO_RETRY,
            )

        self._sandbox_up = True
        release_after = timedelta(seconds=spec.get("review_idle_release_s", 600))
        prompt = prompts.initial_prompt(spec["instruction"], checks)
        turn, fixes, session_id = 0, 0, None
        skip_turn = bool(subtasks)
        unresolved = [r for r in statuses if r["status"] not in {"merged", "unchanged"}]
        while True:
            # A pause takes effect here, between turns, never in the middle of one.
            if self._pause_requested and not skip_turn:
                await self._release_sandbox(task_id, turn)
                await self._set_state(task_id, "PAUSED")
                await workflow.wait_condition(lambda: not self._pause_requested)
            await self._restore_sandbox(task_id, turn)
            turn += 1
            if not skip_turn:
                await self._set_state(task_id, "RUNNING")
                turn_result: TurnResult = await workflow.execute_activity(
                    "run_agent_turn",
                    TurnInput(task_id, turn, prompt, session_id),
                    result_type=TurnResult,
                    start_to_close_timeout=turn_timeout,
                    heartbeat_timeout=timedelta(seconds=spec.get("turn_heartbeat_timeout_s", 120)),
                    retry_policy=_retry(2),
                )
                session_id = turn_result.session_id
                await workflow.execute_activity(
                    "checkpoint_workspace",
                    StepInput(task_id, turn),
                    result_type=CheckpointResult,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=_retry(3),
                )
            skip_turn = False

            over_budget: str | None = await workflow.execute_activity(
                "check_budget", StepInput(task_id, turn), **_QUICK
            )
            if over_budget:
                # The work so far is committed and stays on the task's branch for inspection.
                await self._set_state(task_id, "FAILED", f"budget exceeded: {over_budget}")
                raise ApplicationError(over_budget, type="BudgetExceeded", non_retryable=True)

            await self._set_state(task_id, "VERIFYING")
            verify: VerifyResult = await workflow.execute_activity(
                "run_verification",
                StepInput(task_id, turn),
                result_type=VerifyResult,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=10),
                retry_policy=_NO_RETRY,
            )
            if not verify.passed and fixes < max_fixes:
                fixes += 1
                prompt = prompts.fix_prompt(
                    " && ".join(checks), verify.exit_code, verify.output_tail
                )
                continue

            passed = verify.passed
            if passed and ci is not None:
                outcome = await self._run_ci(task_id, turn, ci)
                passed = outcome.passed
                if not passed and fixes < max_fixes:
                    fixes += 1
                    prompt = prompts.ci_fix_prompt(outcome.summary or "", outcome.url)
                    continue

            await workflow.execute_activity(
                "publish",
                PublishInput(task_id, turn, passed and not unresolved),
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=_retry(3),
            )
            await self._set_state(task_id, "AWAITING_REVIEW")
            try:
                await workflow.wait_condition(lambda: bool(self._reviews), timeout=release_after)
            except TimeoutError:
                # Nobody is looking: stop holding a sandbox. It is rebuilt if work continues.
                await self._release_sandbox(task_id, turn)
                await workflow.wait_condition(lambda: bool(self._reviews))
            decision, feedback = self._reviews.pop(0)
            self._reviews.clear()

            if decision == "approve":
                await self._set_state(task_id, "SUCCEEDED")
                return "SUCCEEDED"
            if decision == "reject":
                await self._set_state(task_id, "CANCELLED", feedback or "rejected by reviewer")
                return "CANCELLED"
            details = "\n".join(
                f"- {r['name']} ({r['status']}): /workspace/.sca/subtask-{r['name']}.patch"
                for r in unresolved
            )
            if details:
                feedback = (
                    (feedback or "") + "\nUnmerged subtasks; resolve their patches:\n" + details
                )
            prompt, fixes = prompts.feedback_prompt(feedback or ""), 0
            unresolved = []


@workflow.defn(name="CodingSubtaskWorkflow")
class CodingSubtaskWorkflow:
    @workflow.run
    async def run(self, task_id: str, subtask: dict, spec: dict) -> SubtaskResult:
        name = subtask["name"]
        result = SubtaskResult(name, branch=f"sca/{task_id[:8]}-{name}")

        async def step(action: str, turn: int = 0, prompt: str = "", session=None) -> dict:
            return await workflow.execute_activity(
                "subtask_step",
                SubtaskInput(task_id, name, action, turn, prompt, session),
                start_to_close_timeout=timedelta(seconds=max(spec["turn_timeout_s"] + 60, 1800)),
                heartbeat_timeout=timedelta(seconds=spec.get("turn_heartbeat_timeout_s", 120)),
                retry_policy=_NO_RETRY
                if action in {"run_verification", "run_setup"}
                else _retry(3),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )

        checks = [c for c in [spec.get("verify_command"), *spec.get("verify_commands", [])] if c]
        prompt = prompts.initial_prompt(
            f"Overall task context:\n{spec['instruction']}\n\n"
            f"Your subtask ({name}):\n{subtask['instruction']}",
            checks,
        )
        session = None
        try:
            await step("prepare")
            if spec.get("setup_command"):
                await step("run_setup")
            for turn in range(1, spec["max_fix_attempts"] + 2):
                budget = await step("check_budget", turn)
                if budget.get("reason"):
                    raise ApplicationError(
                        budget["reason"], type="BudgetExceeded", non_retryable=True
                    )
                response = await step("run_agent_turn", turn, prompt, session)
                session = response["session_id"]
                checkpoint = await step("checkpoint_workspace", turn)
                result.commit, result.changed = checkpoint["commit"], checkpoint["changed"]
                budget = await step("check_budget", turn)
                if budget.get("reason"):
                    raise ApplicationError(
                        budget["reason"], type="BudgetExceeded", non_retryable=True
                    )
                verify = await step("run_verification", turn)
                result.passed = verify["passed"]
                if result.passed:
                    break
                prompt = prompts.fix_prompt(
                    " && ".join(checks), verify["exit_code"], verify["output_tail"]
                )
        except (ActivityError, ApplicationError) as exc:
            if is_cancelled_exception(exc):
                raise
            cause = exc.cause if isinstance(exc, ActivityError) else exc
            result.reason = str(cause)
        finally:
            await step("destroy_sandbox")
        return result
