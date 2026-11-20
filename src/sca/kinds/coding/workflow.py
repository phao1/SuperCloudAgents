"""The coding task workflow: the order of the steps (design.md §7.2).

No I/O here. Everything that touches the outside world is an activity.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
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
    from sca.kernel.workflow import TaskWorkflow
    from sca.kinds.coding.activities import (
        SCM_ATTEMPTS,
        CheckpointResult,
        CiOutcome,
        CiRequest,
        PublishInput,
        ScmInput,
        ScmPhase,
        SetStateInput,
        StepInput,
        TurnInput,
        TurnResult,
        VerifyResult,
        WorkspaceInfo,
    )
    from sca.kinds.coding.best_of import (
        SELECT_DECISION_PREFIX,
        Attempt,
        AttemptStep,
        Candidate,
        RecordAttempt,
        SelectAttempt,
        choose_candidate,
    )
    from sca.kinds.coding.subtask_inputs import MergeInput, PlanningInput, SubtaskInput
    from sca.workspace.merge import SubtaskResult

WORKFLOW_NAME = "CodingTaskWorkflow"
REVIEW_SIGNAL = "submit_review"

_QUICK = {"start_to_close_timeout": timedelta(seconds=30)}
_NO_RETRY = RetryPolicy(maximum_attempts=1)


def _retry(attempts: int) -> RetryPolicy:
    return RetryPolicy(maximum_attempts=attempts, initial_interval=timedelta(seconds=2))


@workflow.defn(name=WORKFLOW_NAME)
class CodingTaskWorkflow(TaskWorkflow):
    def __init__(self) -> None:
        super().__init__()
        self._candidate_attempts: set[int] = set()
        self._selected_attempt: tuple[int, str | None] | None = None
        self._scm_review = False

    @workflow.signal(name="ci_result")
    async def ci_result(
        self, commit: str, passed: bool, summary: str | None = None, url: str | None = None
    ) -> None:
        await self._extension_signal("ci_result", commit, [commit, passed, summary, url])

    @workflow.signal(name=REVIEW_SIGNAL)
    async def submit_review(self, decision: str, feedback: str | None = None) -> None:
        if decision.startswith(SELECT_DECISION_PREFIX):
            attempt = decision.removeprefix(SELECT_DECISION_PREFIX)
            if self._selected_attempt is None and attempt in {str(n) for n in self._candidate_attempts}:
                self._selected_attempt = (int(attempt), feedback)
            return
        await super().submit_review(decision, feedback)

    @workflow.run
    async def run(self, task_id: str) -> str:
        return await super().run(task_id)

    # SCM
    async def _set_state(self, task_id: str, state: str, reason: str | None = None) -> None:
        if self._scm_review and state in {"SUCCEEDED", "CANCELLED"}:
            await self._sync_scm(task_id, "approve" if state == "SUCCEEDED" else "reject")
        # Earlier histories return None from set_state and need no further activity.
        enabled = await workflow.execute_activity(
            "set_state", SetStateInput(task_id, state, reason), **_QUICK,
        )
        if state == "AWAITING_REVIEW":
            self._scm_review = bool(enabled)
            if enabled:
                await self._sync_scm(task_id, "review")
        elif enabled and state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            await self._sync_scm(task_id, "terminal")

    async def _finish(self, task_id: str, state: str, reason: str) -> None:
        self._scm_review = False  # An external cancellation is distinct from a review decision.
        await super()._finish(task_id, state, reason)

    async def _sync_scm(self, task_id: str, phase: ScmPhase) -> None:
        try:
            await workflow.execute_activity(
                "coding.sync_scm", ScmInput(task_id, phase),
                start_to_close_timeout=timedelta(minutes=1), retry_policy=_retry(SCM_ATTEMPTS),
            )
        except ActivityError as exc:
            if is_cancelled_exception(exc):
                raise
            workflow.logger.warning("SCM activity %s failed for task %s", phase, task_id)
    # Best-of-N starts with an already verified checkpoint, so delivery can run even after
    # the candidate turn budgets are spent. The normal workflow owns single-agent tasks.
    async def _run(self, task_id: str) -> str:
        if workflow.patched("coding-best-of"):
            spec = await workflow.execute_activity("load_task", task_id, **_QUICK)
            if spec.get("best_of") is not None:
                try:
                    return await self._run_best_of(task_id, spec)
                finally:
                    try:
                        await workflow.execute_activity(
                            "coding.best_of_cleanup", task_id,
                            start_to_close_timeout=timedelta(minutes=5), retry_policy=_retry(3),
                        )
                    except ActivityError:
                        workflow.logger.exception("could not clean up Best-of-N workspaces")
        return await super()._run(task_id)

    async def _run_best_of(self, task_id: str, spec: dict) -> str:
        self._admission = spec.get("_admission", False)
        await self._acquire_slot(task_id)
        await self._set_state(task_id, "PROVISIONING")
        await workflow.execute_activity(
            "coding.prepare_workspace", task_id, result_type=WorkspaceInfo,
            start_to_close_timeout=timedelta(minutes=10), retry_policy=_retry(3),
        )
        intents, notes = {}, ""
        if spec.get("intents"):
            intents = await workflow.execute_activity("compile_intents", task_id, **_QUICK)
        if spec.get("memory_enabled"):
            notes = await workflow.execute_activity("memory_initial_prompt", task_id, **_QUICK)
        spec["_context_notes"] = "\n".join([*intents.get("notes", []), notes])
        await workflow.execute_activity(
            "create_sandbox", task_id, start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_retry(3),
        )
        if spec.get("setup_command") or intents.get("setup"):
            await workflow.execute_activity(
                "run_setup", task_id, start_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(minutes=5), retry_policy=_NO_RETRY,
            )
        attempts = await workflow.execute_activity(
            "coding.best_of_start", task_id, result_type=list[Attempt],
            start_to_close_timeout=timedelta(minutes=15), retry_policy=_retry(3),
        )
        await self._set_state(task_id, "RUNNING")

        async def run_attempt(attempt: Attempt) -> Candidate:
            try:
                result = await workflow.execute_child_workflow(
                    CodingSubtaskWorkflow.run, args=[task_id, {"name": attempt.name,
                                                             "attempt": asdict(attempt)}, spec],
                    id=f"task-{task_id}-{attempt.name}",
                    cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
            except ChildWorkflowError as exc:
                if is_cancelled_exception(exc):
                    raise
                result = SubtaskResult(attempt.name, reason=str(exc.cause))
            return await workflow.execute_activity(
                "coding.best_of_record", RecordAttempt(task_id, attempt, result), result_type=Candidate,
                start_to_close_timeout=timedelta(minutes=5), retry_policy=_retry(3),
            )

        results = await asyncio.gather(*(run_attempt(attempt) for attempt in attempts),
                                       return_exceptions=True)
        candidates = []
        for result in results:
            if isinstance(result, BaseException):
                raise result
            candidates.append(result)
        chosen = choose_candidate(candidates)
        if chosen is None:
            await self._set_state(task_id, "FAILED", "No Best-of-N attempt produced a checkpoint")
            return "FAILED"
        await self._set_state(task_id, "VERIFYING")
        actor = None
        if spec["best_of"]["select"] == "human":
            self._candidate_attempts = {c.attempt for c in candidates if c.patch_artifact is not None}
            await self._set_state(task_id, "AWAITING_REVIEW")
            await workflow.wait_condition(lambda: self._selected_attempt is not None)
            assert self._selected_attempt is not None
            attempt, actor = self._selected_attempt
            chosen = next(c for c in candidates if c.attempt == attempt)
            await self._acquire_slot(task_id)
            await self._set_state(task_id, "RUNNING")
            await self._set_state(task_id, "VERIFYING")
        await workflow.execute_activity(
            "coding.best_of_adopt", SelectAttempt(task_id, chosen.attempt, actor),
            start_to_close_timeout=timedelta(minutes=10), retry_policy=_retry(3),
        )
        return await self._review_best_of(task_id, spec, chosen, bool(intents))

    async def _review_best_of(self, task_id: str, spec: dict, chosen: Candidate, intents: bool) -> str:
        turn, passed, session = chosen.turns, chosen.passed, None
        if passed and spec.get("ci") is not None:
            passed = (await self._extension("coding.after_verify", task_id, spec, turn))["passed"]
        while True:
            await workflow.execute_activity(
                "coding.publish", PublishInput(task_id, turn, passed),
                start_to_close_timeout=timedelta(minutes=5), retry_policy=_retry(3),
            )
            if self._sandbox_up and spec.get("memory_enabled"):
                await workflow.execute_activity("collect_memory_proposals", StepInput(task_id, turn), **_QUICK)
            if spec.get("acceptance_files"):
                await workflow.execute_activity(
                    "run_acceptance", task_id,
                    start_to_close_timeout=timedelta(seconds=spec["turn_timeout_s"] + 660),
                    heartbeat_timeout=timedelta(minutes=5), retry_policy=_retry(3),
                )
            await self._set_state(task_id, "AWAITING_REVIEW")
            if spec.get("auto_finish"):
                state = "SUCCEEDED" if passed else "FAILED"
                await self._set_state(task_id, state, None if passed else "verification failed")
                return state
            if self._sandbox_up:
                try:
                    await workflow.wait_condition(
                        lambda: bool(self._reviews),
                        timeout=timedelta(seconds=spec.get("review_idle_release_s", 600)),
                    )
                except TimeoutError:
                    await self._release_sandbox(task_id, turn)
            await workflow.wait_condition(lambda: bool(self._reviews))
            decision, feedback = self._reviews.pop(0)
            self._reviews.clear()
            if decision == "approve":
                await self._set_state(task_id, "SUCCEEDED")
                if spec.get("memory_enabled"):
                    try:
                        await workflow.execute_activity(
                            "distill_memories", task_id,
                            start_to_close_timeout=timedelta(minutes=2), retry_policy=_retry(2),
                        )
                    except ActivityError:
                        workflow.logger.warning("memory distillation failed for %s", task_id)
                return "SUCCEEDED"
            if decision == "reject":
                await self._set_state(task_id, "CANCELLED", feedback or "rejected by reviewer")
                return "CANCELLED"
            turn, passed, session = await self._revise_best_of(task_id, spec, turn, session, feedback, intents)

    async def _revise_best_of(
        self, task_id: str, spec: dict, turn: int, session: str | None, feedback: str | None, intents: bool
    ) -> tuple[int, bool, str | None]:
        prompt = await workflow.execute_activity(
            "render_feedback_prompt", args=[spec, feedback or "", []], **_QUICK,
        )
        if session is None:
            prompt += "\n\nOriginal task:\n" + spec["instruction"]
        for fix in range(spec["max_fix_attempts"] + 1):
            if self._pause_requested:
                await self._release_sandbox(task_id, turn)
                await self._set_state(task_id, "PAUSED")
                await workflow.wait_condition(lambda: not self._pause_requested)
            await self._check_budget(task_id, turn)
            await self._restore_sandbox(task_id, turn)
            await self._set_state(task_id, "RUNNING")
            turn += 1
            response = await workflow.execute_activity(
                "run_agent_turn", TurnInput(task_id, turn, prompt, session), result_type=TurnResult,
                start_to_close_timeout=timedelta(seconds=spec["turn_timeout_s"] + 60),
                heartbeat_timeout=timedelta(seconds=spec.get("turn_heartbeat_timeout_s", 120)),
                retry_policy=_retry(2),
            )
            session = response.session_id
            await workflow.execute_activity(
                "coding.checkpoint_workspace", StepInput(task_id, turn), result_type=CheckpointResult,
                start_to_close_timeout=timedelta(minutes=2), retry_policy=_retry(3),
            )
            await self._check_budget(task_id, turn)
            await self._set_state(task_id, "VERIFYING")
            verify_steps = ["coding.run_verification", "verify_intents"] if intents else ["coding.run_verification"]
            results = await asyncio.gather(*(
                workflow.execute_activity(
                    activity_name, StepInput(task_id, turn), result_type=VerifyResult,
                    start_to_close_timeout=timedelta(minutes=30),
                    heartbeat_timeout=timedelta(minutes=10), retry_policy=_NO_RETRY,
                )
                for activity_name in verify_steps
            ))
            passed = all(result.passed for result in results)
            if passed and spec.get("ci") is not None:
                ci = await self._extension("coding.after_verify", task_id, spec, turn)
                passed, prompt = ci["passed"], ci["prompt"]
            elif not passed and fix < spec["max_fix_attempts"]:
                prompt = await workflow.execute_activity(
                    "render_fix_prompt",
                    args=[spec, next(r.exit_code for r in results if not r.passed),
                          "\n".join(r.output_tail for r in results)], **_QUICK,
                )
            if passed or fix == spec["max_fix_attempts"]:
                return turn, passed, session
        raise AssertionError("No revision turn ran")


@workflow.defn(name="coding.before_turn")
class CodingBeforeTurn(TaskWorkflow):
    @workflow.run
    async def run(self, task_id: str, spec: dict) -> dict:
        self._admission = spec.get("_admission", False)
        turn_timeout = timedelta(seconds=spec["turn_timeout_s"] + 60)
        subtasks = spec.get("subtasks", [])
        if spec.get("plan") == "auto":
            await self._set_state(task_id, "RUNNING")
            attempt, feedback = 0, None
            while True:
                attempt += 1
                try:
                    plan = await workflow.execute_activity(
                        "coding.plan_task",
                        PlanningInput(task_id, attempt, feedback),
                        start_to_close_timeout=turn_timeout + timedelta(minutes=15),
                        heartbeat_timeout=timedelta(minutes=5),
                        retry_policy=_NO_RETRY,
                    )
                except ActivityError:
                    await workflow.execute_activity(
                        "coding.reject_plan",
                        PlanningInput(task_id, attempt, "planning activity failed"),
                        **_QUICK,
                    )
                    plan = None
                if not plan or not plan["split"]:
                    break
                if spec.get("plan_review"):
                    # The existing state graph enters review through VERIFYING.
                    await self._set_state(task_id, "VERIFYING")
                    if self._admission:
                        await workflow.execute_activity("release_slot", task_id, **_QUICK)
                    await self._set_state(task_id, "AWAITING_REVIEW")
                    await workflow.wait_condition(lambda: bool(self._reviews))
                    decision, feedback = self._reviews.pop(0)
                    if decision == "reject":
                        await self._set_state(task_id, "CANCELLED", feedback)
                        return {"cancelled": True}
                    await self._acquire_slot(task_id)
                    if decision == "request_changes":
                        await self._set_state(task_id, "RUNNING")
                        continue
                subtasks = plan["subtasks"]
                break
        statuses = []
        if subtasks:
            await workflow.execute_activity(
                "coding.prepare_mirror",
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
                "coding.merge_subtasks",
                MergeInput(task_id, results),
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=_retry(3),
            )
            await workflow.execute_activity(
                "coding.build_merged_workspace",
                task_id,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=_retry(3),
            )
        else:
            await workflow.execute_activity(
                "coding.prepare_workspace",
                task_id,
                result_type=WorkspaceInfo,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=_retry(3),
            )
        return {
            "skip_turn": bool(subtasks),
            "unresolved": [r for r in statuses if r["status"] not in {"merged", "unchanged"}],
        }


@workflow.defn(name="coding.after_verify")
class CodingAfterVerify(TaskWorkflow):
    def __init__(self) -> None:
        super().__init__()
        self._ci_results: dict[str, tuple[bool, str | None, str | None]] = {}

    @workflow.signal(name="ci_result")
    def ci_result(
        self, commit: str, passed: bool, summary: str | None = None, url: str | None = None
    ) -> None:
        # Late reports for an older commit cannot pass the current commit.
        self._ci_results[commit] = (passed, summary, url)

    async def _run_ci(self, task_id: str, turn: int, ci: dict) -> CiOutcome:
        request: CiRequest = await workflow.execute_activity(
            "coding.request_ci",
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
        await workflow.execute_activity("coding.record_ci", outcome, **_QUICK)
        return outcome

    @workflow.run
    async def run(self, task_id: str, spec: dict, turn: int) -> dict:
        outcome = await self._run_ci(task_id, turn, spec["ci"])
        return {
            "passed": outcome.passed,
            "prompt": await workflow.execute_activity(
                "coding.ci_fix_prompt", args=[outcome.summary or "", outcome.url, spec.get("prompt_version")], **_QUICK
            ),
        }


@workflow.defn(name="CodingSubtaskWorkflow")
class CodingSubtaskWorkflow:
    @workflow.run
    async def run(self, task_id: str, subtask: dict, spec: dict) -> SubtaskResult:
        name = subtask["name"]
        result = SubtaskResult(name, branch=f"sca/{task_id[:8]}-{name}")
        attempt = Attempt(**subtask["attempt"]) if "attempt" in subtask else None

        async def step(action: str, turn: int = 0, prompt: str = "", session=None) -> dict:
            inp = SubtaskInput(task_id, name, action, turn, prompt, session)
            no_retry = {"run_verification", "run_setup"}
            if attempt is not None:
                no_retry.update({"prepare", "run_agent_turn"})
            return await workflow.execute_activity(
                "coding.best_of_step" if attempt is not None else "coding.subtask_step",
                AttemptStep(attempt, inp) if attempt is not None else inp,
                start_to_close_timeout=timedelta(seconds=max(spec["turn_timeout_s"] + 60, 1800)),
                heartbeat_timeout=timedelta(seconds=spec.get("turn_heartbeat_timeout_s", 120)),
                retry_policy=_NO_RETRY if action in no_retry else _retry(3),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )

        if attempt is None:
            prompt = await workflow.execute_activity("coding.subtask_prompt", args=[spec, subtask], **_QUICK)
        else:
            prompt = await workflow.execute_activity("render_initial_prompt", spec, **_QUICK)
        prompt += "\n" + spec.get("_context_notes", "")
        session = None
        try:
            await step("prepare")
            if attempt is None and (spec.get("setup_command") or spec.get("_intent_setup")):
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
                if attempt is None:
                    budget = await step("check_budget", turn)
                    if budget.get("reason"):
                        raise ApplicationError(
                            budget["reason"], type="BudgetExceeded", non_retryable=True
                        )
                verify = await step("run_verification", turn)
                result.passed = verify["passed"]
                if result.passed:
                    break
                prompt = await workflow.execute_activity(
                    "render_fix_prompt" if attempt is not None else "coding.fix_prompt",
                    args=[spec, verify["exit_code"], verify["output_tail"]],
                    **_QUICK,
                )
                prompt += "\n" + spec.get("_context_notes", "")
        except (ActivityError, ApplicationError) as exc:
            if is_cancelled_exception(exc):
                raise
            cause = exc.cause if isinstance(exc, ActivityError) else exc
            result.reason = str(cause)
        finally:
            try:
                if attempt is None and spec.get("_context_notes") is not None and spec.get("memory_enabled"):
                    await step("collect_memory_proposals")
            finally:
                await step("destroy_sandbox")
        return result
