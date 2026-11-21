"""The coding task workflow: the order of the steps (design.md §7.2).

No I/O here. Everything that touches the outside world is an activity.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
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

from sca.orchestration.provider_errors import patched_provider_account_error

with workflow.unsafe.imports_passed_through():
    from sca.intents.base import TICKET_TYPES
    from sca.kernel.interjection import MESSAGES_PENDING, Guidance, TurnInterrupted, with_guidance
    from sca.kernel.workflow import ProjectCheckpoints, TaskWorkflow
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
    from sca.kinds.coding.spec import subtask_order
    from sca.kinds.coding.subtask_inputs import (
        MergeInput,
        PlanningInput,
        SubtaskInput,
        SubtaskPreparationInput,
    )
    from sca.workspace.merge import MERGED_STATUSES, SubtaskResult

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
        try:
            return await super().run(task_id)
        finally:
            await self._write_ticket_outcome(task_id)

    async def _write_ticket_outcome(self, task_id: str) -> None:
        # When cancellation interrupts load_task, the activity can still read the persisted spec.
        if self._task_spec is not None and not any(
            source.get("type") in TICKET_TYPES and source.get("params", {}).get("writeback", True)
            for source in self._task_spec.get("intents", [])
        ):
            return
        if workflow.patched("coding-ticket-writeback"):
            try:
                await workflow.execute_activity(
                    "coding.write_ticket_outcome", task_id,
                    start_to_close_timeout=timedelta(minutes=3), retry_policy=_NO_RETRY,
                )
            except ActivityError:
                workflow.logger.warning("could not record ticket write-back for %s", task_id)

    # SCM
    async def _set_state(self, task_id: str, state: str, reason: str | None = None,
                         *, model_provider_error: dict | None = None,
                         wait_for_messages: bool = False) -> bool:
        decision = {"SUCCEEDED": "approve", "CANCELLED": "reject"}.get(state)
        if self._scm_review and decision and not wait_for_messages:
            await self._sync_scm(task_id, decision)
        # Earlier histories return None from set_state and need no further activity.
        enabled = await workflow.execute_activity(
            "set_state", SetStateInput(task_id, state, reason, model_provider_error, wait_for_messages),
            **_QUICK,
        )
        if enabled == MESSAGES_PENDING:
            return False
        # The database rejects approval under the task lock if guidance is waiting.
        if self._scm_review and decision and wait_for_messages:
            await self._sync_scm(task_id, decision)
        if state == "AWAITING_REVIEW":
            self._scm_review = bool(enabled)
            if enabled:
                await self._sync_scm(task_id, "review")
        elif enabled and state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            await self._sync_scm(task_id, "terminal")
        return True

    async def _finish(self, task_id: str, state: str, reason: str,
                      *, model_provider_error: dict | None = None) -> None:
        self._scm_review = False  # An external cancellation is distinct from a review decision.
        await super()._finish(task_id, state, reason, model_provider_error=model_provider_error)

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
        if spec.get("_user_interjection"):
            self._interjections.enable()
        self._admission = spec.get("_admission", False)
        self._project_checkpoints = ProjectCheckpoints(spec)
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
        await self._wait_for_project(task_id, "PROVISIONING")
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
                if is_cancelled_exception(exc) or patched_provider_account_error(exc):
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
        await self._wait_for_project(task_id, "RUNNING")
        await self._set_state(task_id, "VERIFYING")
        actor = None
        if spec["best_of"]["select"] == "human":
            self._candidate_attempts = {c.attempt for c in candidates if c.patch_artifact is not None}
            await self._set_state(task_id, "AWAITING_REVIEW")
            await workflow.wait_condition(lambda: self._selected_attempt is not None)
            assert self._selected_attempt is not None
            attempt, actor = self._selected_attempt
            chosen = next(c for c in candidates if c.attempt == attempt)
            await self._wait_for_project(task_id, "AWAITING_REVIEW")
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
            await self._wait_for_project(task_id, "VERIFYING", turn)
            passed = (await self._extension("coding.after_verify", task_id, spec, turn))["passed"]
        while True:
            await self._wait_for_project(task_id, "VERIFYING", turn)
            if self._interjections.pending:
                turn, passed, session = await self._revise_best_of(
                    task_id, spec, turn, session, None, intents,
                )
            await workflow.execute_activity(
                "coding.publish", PublishInput(task_id, turn, passed),
                start_to_close_timeout=timedelta(minutes=5), retry_policy=_retry(3),
            )
            if self._sandbox_up and spec.get("memory_enabled"):
                await workflow.execute_activity("collect_memory_proposals", StepInput(task_id, turn), **_QUICK)
            if spec.get("acceptance_files"):
                await self._wait_for_project(task_id, "VERIFYING", turn)
                await workflow.execute_activity(
                    "run_acceptance", task_id,
                    start_to_close_timeout=timedelta(seconds=spec["turn_timeout_s"] + 660),
                    heartbeat_timeout=timedelta(minutes=5), retry_policy=_retry(3),
                )
            await self._wait_for_project(task_id, "VERIFYING", turn)
            await self._set_state(task_id, "AWAITING_REVIEW")
            if spec.get("auto_finish"):
                state = "SUCCEEDED" if passed else "FAILED"
                if await self._complete(task_id, state, None if passed else "verification failed"):
                    return state
                continue
            if self._sandbox_up:
                try:
                    await workflow.wait_condition(
                        self._review_ready,
                        timeout=timedelta(seconds=spec.get("review_idle_release_s", 600)),
                    )
                except TimeoutError:
                    await self._release_sandbox(task_id, turn)
            await workflow.wait_condition(self._review_ready)
            await self._wait_for_project(task_id, "AWAITING_REVIEW", turn)
            decision, feedback = self._next_review()
            if decision == "approve" and await self._complete(task_id, "SUCCEEDED"):
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
        fix, limit = 0, spec["max_fix_attempts"]
        while True:
            await self._wait_for_project(task_id, "RUNNING", turn)
            if self._pause_requested:
                await self._release_sandbox(task_id, turn)
                await self._set_state(task_id, "PAUSED")
                await workflow.wait_condition(lambda: not self._pause_requested)
            await self._check_budget(task_id, turn)
            await self._restore_sandbox(task_id, turn)
            await self._set_state(task_id, "RUNNING")
            turn += 1
            messages = await self._interjections.deliver(task_id, turn)
            interrupted = False
            try:
                response = await self._run_agent_turn(task_id, spec, turn, prompt, session, messages)
                session = response.session_id
            except TurnInterrupted:
                interrupted = True
            await workflow.execute_activity(
                "coding.checkpoint_workspace", StepInput(task_id, turn), result_type=CheckpointResult,
                start_to_close_timeout=timedelta(minutes=2), retry_policy=_retry(3),
            )
            if interrupted:
                continue
            if messages and not response.skipped:
                await self._interjections.apply(task_id, messages)
            await self._wait_for_project(task_id, "RUNNING", turn)
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
            elif not passed and fix < limit:
                prompt = await workflow.execute_activity(
                    "render_fix_prompt",
                    args=[spec, next(r.exit_code for r in results if not r.passed),
                          "\n".join(r.output_tail for r in results)], **_QUICK,
                )
            if passed or fix == limit:
                return turn, passed, session
            fix += 1

    # Task interjection

    def _route_interjections(self, name: str | None) -> bool:
        return name == "coding.before_turn"


@workflow.defn(name="coding.before_turn")
class CodingBeforeTurn(TaskWorkflow):
    @workflow.run
    async def run(self, task_id: str, spec: dict) -> dict:
        if spec.get("_user_interjection"):
            self._interjections.enable()
        self._admission = spec.get("_admission", False)
        self._project_checkpoints = ProjectCheckpoints(spec)
        turn_timeout = timedelta(seconds=spec["turn_timeout_s"] + 60)
        subtasks = spec.get("subtasks", [])
        shared_paths = spec.get("shared_paths", [])
        guidance: list[Guidance] = []
        if spec.get("plan") == "auto":
            await self._set_state(task_id, "RUNNING")
            attempt, feedback = 0, None
            while True:
                await self._wait_for_project(task_id, "RUNNING")
                attempt += 1
                messages = await self._interjections.deliver(task_id, attempt, planning=True)
                try:
                    plan = await self._interjections.run_activity(
                        "coding.plan_task",
                        PlanningInput(task_id, attempt, feedback,
                                      [message.text for message in [*guidance, *messages]],
                                      self._interjections.enabled),
                        start_to_close_timeout=turn_timeout + timedelta(minutes=15),
                        heartbeat_timeout=timedelta(minutes=5),
                        retry_policy=_NO_RETRY,
                    )
                except TurnInterrupted:
                    continue
                except ActivityError as exc:
                    if patched_provider_account_error(exc):
                        raise
                    await workflow.execute_activity(
                        "coding.reject_plan",
                        PlanningInput(task_id, attempt, "planning activity failed"),
                        **_QUICK,
                    )
                    plan = None
                if plan:
                    await self._interjections.apply(task_id, messages)
                    guidance.extend(messages)
                    if self._interjections.pending:
                        continue
                if not plan or not plan["split"]:
                    break
                if spec.get("plan_review"):
                    # The existing state graph enters review through VERIFYING.
                    await self._set_state(task_id, "VERIFYING")
                    if self._admission:
                        await workflow.execute_activity("release_slot", task_id, **_QUICK)
                    await self._set_state(task_id, "AWAITING_REVIEW")
                    await workflow.wait_condition(self._review_ready)
                    await self._wait_for_project(task_id, "AWAITING_REVIEW")
                    if self._interjections.pending:
                        decision, feedback = self._next_review()
                    else:
                        decision, feedback = self._reviews.pop(0)
                    if decision == "reject":
                        await self._set_state(task_id, "CANCELLED", feedback)
                        return {"cancelled": True}
                    await self._acquire_slot(task_id)
                    if decision == "request_changes":
                        await self._set_state(task_id, "RUNNING")
                        continue
                subtasks = plan["subtasks"]
                shared_paths = plan.get("shared_paths", [])
                break
        statuses = []
        if subtasks:
            await self._wait_for_project(task_id, "RUNNING")
            if guidance:
                spec = {**spec, "_context_notes": with_guidance(
                    spec.get("_context_notes", ""), [message.text for message in guidance],
                )}
            await workflow.execute_activity(
                "coding.prepare_mirror",
                task_id,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=_retry(3),
            )
            await self._set_state(task_id, "RUNNING")
            has_dependencies = any(sub.get("depends_on") for sub in subtasks)
            scopes = {sub["name"]: sub["paths"] for sub in subtasks if sub.get("paths")}
            scoped = bool(scopes or shared_paths) and workflow.patched("coding-subtask-scopes")
            if scoped:
                spec = {**spec, "_subtask_scopes": scopes, "_shared_paths": shared_paths}
            if scoped or has_dependencies:
                await workflow.execute_activity(
                    "coding.prepare_subtask_base",
                    task_id,
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=_retry(3),
                )
            if has_dependencies:
                results = await _run_subtask_dag(task_id, subtasks, spec)
            else:
                handles = [await _start_subtask(task_id, sub, spec) for sub in subtasks]
                results = await _gather_subtasks(
                    *(_collect_subtask(handle, task_id, sub)
                      for handle, sub in zip(handles, subtasks, strict=True))
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
            "unresolved": [r for r in statuses if r["status"] not in MERGED_STATUSES],
            **({"applied_messages": self._interjections.applied_ids, "guidance": [
                message.text for message in guidance
            ]} if guidance else {}),
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
        attempt = Attempt(**subtask["attempt"]) if "attempt" in subtask else None
        result = SubtaskResult(
            name, branch=f"sca/{task_id[:8]}-{name}", depends_on=subtask.get("depends_on", []),
        )

        async def step(
            action: str, turn: int = 0, prompt: str = "", session: str | None = None,
        ) -> dict:
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
            prompt = await workflow.execute_activity(
                "coding.subtask_prompt", args=[spec, subtask], **_QUICK,
            )
        else:
            prompt = await workflow.execute_activity("render_initial_prompt", spec, **_QUICK)
        context = "\n" + spec.get("_context_notes", "")
        scopes = spec.get("_subtask_scopes", {})
        shared = spec.get("_shared_paths", [])
        dependencies = subtask.get("_dependencies", [])
        session = None
        project = ProjectCheckpoints(spec)
        try:
            await project.wait(task_id)
            if attempt is None and (scopes or shared or dependencies):
                prepared = await workflow.execute_activity(
                    "coding.prepare_scoped_subtask",
                    SubtaskPreparationInput(
                        task_id, name, [SubtaskResult(**item) for item in dependencies], scopes, shared,
                    ),
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=_retry(3),
                    cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                )
                if prepared.get("blocked"):
                    result.status, result.reason = "blocked", prepared["blocked"]
                    return result
                if scopes.get(name):
                    context += "\n\nYour allowed paths (gitignore globs): " + json.dumps(scopes[name])
                    if shared:
                        context += "\nShared files you may also edit: " + json.dumps(shared)
                context += prepared.get("contracts", "")
            else:
                await step("prepare")
            if attempt is None and (spec.get("setup_command") or spec.get("_intent_setup")):
                await project.wait(task_id)
                await step("run_setup")
            for turn in range(1, spec["max_fix_attempts"] + 2):
                await project.wait(task_id)
                budget = await step("check_budget", turn)
                if budget.get("reason"):
                    raise ApplicationError(
                        budget["reason"], type="BudgetExceeded", non_retryable=True
                    )
                response = await step("run_agent_turn", turn, prompt + context, session)
                session = response["session_id"]
                checkpoint = await step("checkpoint_workspace", turn)
                result.commit, result.changed = checkpoint["commit"], checkpoint["changed"]
                await project.wait(task_id)
                if attempt is None:
                    budget = await step("check_budget", turn)
                    if budget.get("reason"):
                        raise ApplicationError(
                            budget["reason"], type="BudgetExceeded", non_retryable=True
                        )
                    result.reason = checkpoint.get("scope_feedback")
                    if result.reason:
                        result.passed = False
                        prompt = result.reason
                        continue
                verify = await step("run_verification", turn)
                result.passed = verify["passed"]
                if result.passed:
                    break
                prompt = await workflow.execute_activity(
                    "render_fix_prompt" if attempt is not None else "coding.fix_prompt",
                    args=[spec, verify["exit_code"], verify["output_tail"]],
                    **_QUICK,
                )
        except (ActivityError, ApplicationError) as exc:
            if is_cancelled_exception(exc) or patched_provider_account_error(exc):
                raise
            cause = exc.cause if isinstance(exc, ActivityError) else exc
            result.reason = str(cause)
        finally:
            if result.status != "blocked":
                try:
                    if (attempt is None and spec.get("_context_notes") is not None
                            and spec.get("memory_enabled")):
                        await step("collect_memory_proposals")
                finally:
                    await step("destroy_sandbox")
        return result


# Phase 6 subtask dependency scheduling
async def _start_subtask(
    task_id: str, subtask: dict, spec: dict,
) -> asyncio.Future[SubtaskResult]:
    return await workflow.start_child_workflow(
        CodingSubtaskWorkflow.run,
        args=[task_id, subtask, spec],
        id=f"task-{task_id}-sub-{subtask['name']}",
        cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
    )


async def _collect_subtask(
    handle: asyncio.Future[SubtaskResult], task_id: str, subtask: dict,
) -> SubtaskResult:
    try:
        return await handle
    except ChildWorkflowError as exc:
        if is_cancelled_exception(exc) or patched_provider_account_error(exc):
            raise
        return SubtaskResult(
            subtask["name"],
            branch=f"sca/{task_id[:8]}-{subtask['name']}",
            reason=str(exc.cause),
            depends_on=subtask.get("depends_on", []),
        )


async def _gather_subtasks(*steps: Awaitable[SubtaskResult]) -> list[SubtaskResult]:
    running = [asyncio.ensure_future(step) for step in steps]
    try:
        return await asyncio.gather(*running)
    except ChildWorkflowError as exc:
        if patched_provider_account_error(exc):
            # Let the other child workflows release their sandboxes and virtual keys.
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
        raise


async def _run_subtask_dag(task_id: str, subtasks: list[dict], spec: dict) -> list[SubtaskResult]:
    by_name = {sub["name"]: sub for sub in subtasks}
    dependencies = {name: sub.get("depends_on", []) for name, sub in by_name.items()}
    running: dict[str, asyncio.Task[SubtaskResult]] = {}

    async def run(name: str) -> SubtaskResult:
        prerequisites = [await running[need] for need in dependencies[name]]
        failed = [result.name for result in prerequisites if not result.passed]
        if failed:
            return SubtaskResult(
                name, status="blocked", depends_on=dependencies[name],
                reason="Dependencies did not pass: " + ", ".join(failed),
            )
        subtask = by_name[name]
        if prerequisites:
            subtask = {**subtask, "_dependencies": [asdict(result) for result in prerequisites]}
        handle = await _start_subtask(task_id, subtask, spec)
        return await _collect_subtask(handle, task_id, subtask)

    for name in subtask_order(dependencies):
        running[name] = asyncio.create_task(run(name))
    return await _gather_subtasks(*running.values())
