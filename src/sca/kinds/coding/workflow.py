"""The coding task workflow: the order of the steps (design.md §7.2).

No I/O here. Everything that touches the outside world is an activity.
"""

from __future__ import annotations

import asyncio
import json
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
        CiOutcome,
        CiRequest,
        StepInput,
        WorkspaceInfo,
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
    @workflow.signal(name="ci_result")
    async def ci_result(
        self, commit: str, passed: bool, summary: str | None = None, url: str | None = None
    ) -> None:
        await self._extension_signal("ci_result", commit, [commit, passed, summary, url])

    @workflow.run
    async def run(self, task_id: str) -> str:
        return await super().run(task_id)


@workflow.defn(name="coding.before_turn")
class CodingBeforeTurn(TaskWorkflow):
    @workflow.run
    async def run(self, task_id: str, spec: dict) -> dict:
        self._admission = spec.get("_admission", False)
        turn_timeout = timedelta(seconds=spec["turn_timeout_s"] + 60)
        subtasks = spec.get("subtasks", [])
        shared_paths = spec.get("shared_paths", [])
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
                shared_paths = plan.get("shared_paths", [])
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
                results = await asyncio.gather(
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
        result = SubtaskResult(
            name, branch=f"sca/{task_id[:8]}-{name}", depends_on=subtask.get("depends_on", []),
        )

        async def step(action: str, turn: int = 0, prompt: str = "", session=None) -> dict:
            return await workflow.execute_activity(
                "coding.subtask_step",
                SubtaskInput(task_id, name, action, turn, prompt, session),
                start_to_close_timeout=timedelta(seconds=max(spec["turn_timeout_s"] + 60, 1800)),
                heartbeat_timeout=timedelta(seconds=spec.get("turn_heartbeat_timeout_s", 120)),
                retry_policy=_NO_RETRY
                if action in {"run_verification", "run_setup"}
                else _retry(3),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )

        prompt = await workflow.execute_activity(
            "coding.subtask_prompt",
            args=[spec, subtask],
            **_QUICK,
        )
        context = "\n" + spec.get("_context_notes", "")
        scopes = spec.get("_subtask_scopes", {})
        shared = spec.get("_shared_paths", [])
        dependencies = [SubtaskResult(**item) for item in subtask.get("_dependencies", [])]
        session = None
        try:
            if scopes or shared or dependencies:
                prepared = await workflow.execute_activity(
                    "coding.prepare_scoped_subtask",
                    SubtaskPreparationInput(task_id, name, dependencies, scopes, shared),
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
            if spec.get("setup_command") or spec.get("_intent_setup"):
                await step("run_setup")
            for turn in range(1, spec["max_fix_attempts"] + 2):
                budget = await step("check_budget", turn)
                if budget.get("reason"):
                    raise ApplicationError(
                        budget["reason"], type="BudgetExceeded", non_retryable=True
                    )
                response = await step("run_agent_turn", turn, prompt + context, session)
                session = response["session_id"]
                checkpoint = await step("checkpoint_workspace", turn)
                result.commit, result.changed = checkpoint["commit"], checkpoint["changed"]
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
                    "coding.fix_prompt",
                    args=[spec, verify["exit_code"], verify["output_tail"]],
                    **_QUICK,
                )
        except (ActivityError, ApplicationError) as exc:
            if is_cancelled_exception(exc):
                raise
            cause = exc.cause if isinstance(exc, ActivityError) else exc
            result.reason = str(cause)
        finally:
            if result.status != "blocked":
                try:
                    if spec.get("_context_notes") is not None and spec.get("memory_enabled"):
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
        if is_cancelled_exception(exc):
            raise
        return SubtaskResult(
            subtask["name"],
            branch=f"sca/{task_id[:8]}-{subtask['name']}",
            reason=str(exc.cause),
            depends_on=subtask.get("depends_on", []),
        )


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
    return await asyncio.gather(*running.values())
