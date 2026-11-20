"""The generic task turn loop and lifecycle.

No I/O here. Everything that touches the outside world is an activity.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    ChildWorkflowError,
    is_cancelled_exception,
)

with workflow.unsafe.imports_passed_through():
    from sca.intents.base import REMOTE_TYPES
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
REVIEW_SIGNAL = "submit_review"

_QUICK = {"start_to_close_timeout": timedelta(seconds=30)}
_NO_RETRY = RetryPolicy(maximum_attempts=1)


def _retry(attempts: int) -> RetryPolicy:
    return RetryPolicy(maximum_attempts=attempts, initial_interval=timedelta(seconds=2))


@workflow.defn(name="TaskWorkflow")
class TaskWorkflow:
    def __init__(self) -> None:
        self._active_extension = None
        self._reviews: list[tuple[str, str | None]] = []
        self._pause_requested = False
        self._sandbox_up = False
        self._admission = False
        self._task_spec: dict | None = None
        self._extension_signals: dict[tuple[str, str], tuple[str, list[Any]]] = {}

    async def _extension_signal(self, name: str, key: str, args: list[Any]) -> None:
        self._extension_signals[name, key] = (name, args)
        if self._active_extension:
            await self._active_extension.signal(name, args=args)

    @workflow.signal(name="pause")
    def pause(self) -> None:
        self._pause_requested = True

    @workflow.signal(name="resume")
    def resume(self) -> None:
        self._pause_requested = False

    @workflow.signal(name=REVIEW_SIGNAL)
    async def submit_review(self, decision: str, feedback: str | None = None) -> None:
        if self._active_extension:
            await self._active_extension.signal(REVIEW_SIGNAL, args=[decision, feedback])
        else:
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
            elif isinstance(e, (ActivityError, ChildWorkflowError)):
                failure = e
                while isinstance(failure, ChildWorkflowError):
                    failure = failure.cause
                cause = failure.cause or failure
                reason = f"{getattr(failure, 'activity_type', 'extension')}: {getattr(cause, 'message', None) or cause}"
                await self._finish(task_id, "FAILED", reason)
                raise ApplicationError(reason, type="TaskFailed", non_retryable=True) from e
            raise
        finally:
            await self._destroy_sandbox(task_id)
            if self._admission:
                await workflow.execute_activity("release_slot", task_id, **_QUICK)

    async def _acquire_slot(self, task_id: str) -> None:
        if not self._admission:
            return
        delay = 1.0
        while True:
            result = await workflow.execute_activity("acquire_slot", task_id, **_QUICK)
            if result.get("granted"):
                return
            if result.get("terminal"):
                raise ApplicationError("Task is terminal", non_retryable=True)
            await workflow.sleep(delay * workflow.random().uniform(0.8, 1.0))
            delay = min(10.0, delay * 2)

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
        await self._acquire_slot(task_id)
        if not self._sandbox_up:
            await workflow.execute_activity(
                "restore_sandbox",
                StepInput(task_id, turn),
                start_to_close_timeout=timedelta(minutes=20),
                heartbeat_timeout=timedelta(minutes=5),
                retry_policy=_retry(3),
            )
            self._sandbox_up = True

    async def _check_budget(self, task_id: str, turn: int) -> None:
        reason = await workflow.execute_activity("check_budget", StepInput(task_id, turn), **_QUICK)
        if reason:
            await self._set_state(task_id, "FAILED", f"budget exceeded: {reason}")
            raise ApplicationError(reason, type="BudgetExceeded", non_retryable=True)

    async def _run(self, task_id: str) -> str:
        spec: dict = await workflow.execute_activity("load_task", task_id, **_QUICK)
        self._task_spec = spec
        spec.setdefault("_kind", "analysis" if "question" in spec else "coding")
        max_fixes: int = spec["max_fix_attempts"]
        turn_timeout = timedelta(seconds=spec["turn_timeout_s"] + 60)

        self._admission = spec.get("_admission", False)
        await self._acquire_slot(task_id)
        early_context = workflow.patched("memory-before-planning")
        intents = {}
        memory_note = ""
        intent_options = (
            {"start_to_close_timeout": timedelta(minutes=4), "retry_policy": _retry(2)}
            if any(source.get("type") in REMOTE_TYPES for source in spec.get("intents", []))
            else _QUICK
        )
        if early_context:
            if spec.get("memory_enabled"):
                memory_note = await workflow.execute_activity("memory_initial_prompt", task_id, **_QUICK)
            if spec.get("intents"):
                if any(source.get("path") for source in spec["intents"]):
                    await workflow.execute_activity(
                        spec["hooks"]["prepare"], task_id,
                        start_to_close_timeout=timedelta(minutes=10), retry_policy=_retry(3),
                    )
                intents = await workflow.execute_activity("compile_intents", task_id, **intent_options)
            spec["_context_notes"] = "\n".join([*intents.get("notes", []), memory_note])
            spec["_intent_setup"] = bool(intents.get("setup"))
        await self._set_state(task_id, "PROVISIONING")
        hooks = spec["hooks"]
        extension = {}
        if hooks.get("extensions") and spec.get("extensions_before", True):
            extension = await self._extension(hooks["extensions"][0], task_id, spec)
            if extension.get("cancelled"):
                return "CANCELLED"
        else:
            await workflow.execute_activity(
                hooks["prepare"],
                task_id,
                result_type=WorkspaceInfo,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=_retry(3),
            )
        if not early_context:
            if spec.get("intents"):
                intents = await workflow.execute_activity("compile_intents", task_id, **intent_options)
            if spec.get("memory_enabled"):
                memory_note = await workflow.execute_activity("memory_initial_prompt", task_id, **_QUICK)
        await workflow.execute_activity(
            "create_sandbox",
            task_id,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_retry(3),
        )
        if spec.get("setup_command") or intents.get("setup"):
            await workflow.execute_activity(
                "run_setup",
                task_id,
                start_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(minutes=5),
                retry_policy=_NO_RETRY,
            )

        self._sandbox_up = True
        release_after = timedelta(seconds=spec.get("review_idle_release_s", 600))
        prompt = await workflow.execute_activity("render_initial_prompt" if spec.get("_versioned_prompts") else hooks["initial_prompt"], spec, **_QUICK)
        prompt += "\n" + "\n".join(intents.get("notes", [])) + "\n" + memory_note
        turn, fixes, session_id = 0, 0, None
        skip_turn = extension.get("skip_turn", False)
        unresolved = extension.get("unresolved", [])
        while True:
            # A pause takes effect here, between turns, never in the middle of one.
            if self._pause_requested and not skip_turn:
                await self._release_sandbox(task_id, turn)
                await self._set_state(task_id, "PAUSED")
                await workflow.wait_condition(lambda: not self._pause_requested)
            await self._restore_sandbox(task_id, turn)
            turn += 1
            if not skip_turn:
                await self._check_budget(task_id, turn - 1)
                await self._set_state(task_id, "RUNNING")
                try:
                    turn_result: TurnResult = await workflow.execute_activity(
                        "run_agent_turn",
                        TurnInput(task_id, turn, prompt, session_id),
                        result_type=TurnResult,
                        start_to_close_timeout=turn_timeout,
                        heartbeat_timeout=timedelta(
                            seconds=spec.get("turn_heartbeat_timeout_s", 120)
                        ),
                        retry_policy=_retry(2),
                    )
                except ActivityError:
                    await self._check_budget(task_id, turn)
                    raise
                session_id = turn_result.session_id
                await workflow.execute_activity(
                    hooks["checkpoint"],
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
            verify_run = workflow.execute_activity(
                hooks["verify"],
                StepInput(task_id, turn),
                result_type=VerifyResult,
                start_to_close_timeout=timedelta(minutes=30),
                heartbeat_timeout=timedelta(minutes=10),
                retry_policy=_NO_RETRY,
            )
            if intents:
                verify, intent_verify = await asyncio.gather(
                    verify_run,
                    workflow.execute_activity(
                        "verify_intents",
                        StepInput(task_id, turn),
                        result_type=VerifyResult,
                        start_to_close_timeout=timedelta(minutes=30),
                        retry_policy=_NO_RETRY,
                        heartbeat_timeout=timedelta(minutes=10),
                    ),
                )
                verify = VerifyResult(
                    verify.passed and intent_verify.passed,
                    verify.exit_code or intent_verify.exit_code,
                    verify.output_tail + "\n" + intent_verify.output_tail,
                )
            else:
                verify = await verify_run
            if not verify.passed and fixes < max_fixes:
                fixes += 1
                prompt = await workflow.execute_activity(
                    "render_fix_prompt" if spec.get("_versioned_prompts") else hooks["fix_prompt"], args=[spec, verify.exit_code, verify.output_tail], **_QUICK
                )
                continue

            passed = verify.passed
            if (
                passed
                and len(hooks.get("extensions", [])) > 1
                and spec.get("extensions_after", False)
            ):
                outcome = await self._extension(hooks["extensions"][1], task_id, spec, turn)
                passed = outcome["passed"]
                if not passed and fixes < max_fixes:
                    fixes += 1
                    prompt = outcome["prompt"]
                    continue

            await workflow.execute_activity(
                hooks["publish"],
                PublishInput(task_id, turn, passed and not unresolved),
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=_retry(3),
            )
            if spec.get('memory_enabled'):
                await workflow.execute_activity(
                    'collect_memory_proposals', StepInput(task_id, turn), **_QUICK)
            if spec.get("acceptance_files"):
                await workflow.execute_activity(
                    "run_acceptance", task_id,
                    start_to_close_timeout=turn_timeout + timedelta(minutes=10),
                    heartbeat_timeout=timedelta(minutes=5), retry_policy=_retry(3),
                )
            # The slot follows the sandbox: a prompt review keeps both, the idle timer below
            # releases both (release_sandbox frees the slot).
            await self._set_state(task_id, "AWAITING_REVIEW")
            if spec.get("auto_finish"):
                state = "SUCCEEDED" if passed and not unresolved else "FAILED"
                await self._set_state(task_id, state, None if state == "SUCCEEDED" else "verification failed")
                return state
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
                if spec.get('memory_enabled'):
                    try:
                        await workflow.execute_activity(
                            'distill_memories', task_id,
                            start_to_close_timeout=timedelta(minutes=2), retry_policy=_retry(2))
                    except ActivityError:
                        workflow.logger.warning('memory distillation failed for %s', task_id)
                return "SUCCEEDED"
            if decision == "reject":
                await self._set_state(task_id, "CANCELLED", feedback or "rejected by reviewer")
                return "CANCELLED"
            prompt = await workflow.execute_activity(
                "render_feedback_prompt" if spec.get("_versioned_prompts") else hooks["feedback_prompt"],
                args=[spec, feedback or "", unresolved] if spec.get("_versioned_prompts") else [feedback or "", unresolved], **_QUICK
            )
            fixes = 0
            unresolved = []

    async def _extension(self, name: str, task_id: str, spec: dict, *args) -> dict:
        self._active_extension = await workflow.start_child_workflow(
            name,
            args=[task_id, spec, *args],
            cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
        )
        try:
            # A child can request review before its start acknowledgement reaches us.
            while self._reviews:
                decision, feedback = self._reviews.pop(0)
                await self._active_extension.signal(REVIEW_SIGNAL, args=[decision, feedback])
            for signal_name, values in self._extension_signals.values():
                await self._active_extension.signal(signal_name, args=values)
            return await self._active_extension
        finally:
            self._active_extension = None
