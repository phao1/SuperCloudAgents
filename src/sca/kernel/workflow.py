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

from sca.orchestration.provider_errors import patched_provider_account_error

with workflow.unsafe.imports_passed_through():
    from sca.core.errors import MODEL_BUDGET_REASONS
    from sca.core.models import MessageMode
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
    from sca.kernel.interjection import (
        CONTINUE_WITH_GUIDANCE,
        MESSAGES_PENDING,
        USER_MESSAGE_SIGNAL,
        Guidance,
        Interjections,
        SettleTurnSpend,
        TurnInterrupted,
        TurnSpendSettlements,
        with_guidance,
    )
REVIEW_SIGNAL = "submit_review"

_QUICK = {"start_to_close_timeout": timedelta(seconds=30)}
_NO_RETRY = RetryPolicy(maximum_attempts=1)


def _retry(attempts: int) -> RetryPolicy:
    return RetryPolicy(maximum_attempts=attempts, initial_interval=timedelta(seconds=2))


# Model gateway budget refusals
def _model_budget_reason(cause: BaseException | None) -> str | None:
    if (isinstance(cause, ApplicationError) and cause.non_retryable and cause.type == "BudgetExceeded"
            and cause.message in MODEL_BUDGET_REASONS):
        return cause.message
    return None


# Projects
async def project_work_blocked(task_id: str) -> bool:
    return await workflow.execute_activity("project_work_blocked", task_id, **_QUICK)


async def wait_for_project_resume(task_id: str) -> None:
    delay = timedelta(seconds=5)
    while await project_work_blocked(task_id):
        await workflow.sleep(delay)


class ProjectCheckpoints:
    def __init__(self, spec: dict) -> None:
        self.enabled: bool | None = spec.get("_project_control", None if spec.get("_versioned_prompts") else False)
        self.position = 0

    def available(self) -> bool:
        if self.enabled is None:
            # Backfilled threads can have a recorded load_task from before project control.
            # An earlier replayed checkpoint must leave later checkpoints eligible.
            self.position += 1
            if workflow.patched(f"projects-checkpoint-{self.position}"):
                self.enabled = True
        return self.enabled is True

    async def wait(self, task_id: str) -> None:
        if self.available():
            await wait_for_project_resume(task_id)


@workflow.defn(name="TaskWorkflow")
class TaskWorkflow:
    def __init__(self) -> None:
        self._active_extension = None
        self._reviews: list[tuple[str, str | None]] = []
        self._pause_requested = False
        self._sandbox_up = False
        self._admission = False
        self._project_checkpoints = ProjectCheckpoints({})
        self._task_spec: dict | None = None
        self._extension_signals: dict[tuple[str, str], tuple[str, list[Any]]] = {}
        self._interjections = Interjections()
        self._turn_spend = TurnSpendSettlements()
        self._extension_name: str | None = None

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

    async def _set_state(self, task_id: str, state: str, reason: str | None = None,
                         *, model_provider_error: dict | None = None,
                         wait_for_messages: bool = False) -> bool:
        result = await workflow.execute_activity(
            "set_state", SetStateInput(task_id, state, reason, model_provider_error, wait_for_messages),
            **_QUICK,
        )
        return result != MESSAGES_PENDING

    @workflow.run
    async def run(self, task_id: str) -> str:
        try:
            return await self._run(task_id)
        except BaseException as e:
            if is_cancelled_exception(e):
                await self._turn_spend.wait()
                await self._finish(task_id, "CANCELLED", "cancelled by user")
            elif isinstance(e, (ActivityError, ChildWorkflowError)):
                await self._turn_spend.wait()
                if provider := patched_provider_account_error(e):
                    await self._finish(task_id, "FAILED", provider.reason,
                                       model_provider_error=provider.payload())
                    raise ApplicationError(provider.reason, type="TaskFailed", non_retryable=True) from e
                failure = e
                while isinstance(failure, ChildWorkflowError):
                    failure = failure.cause
                cause = failure.cause or failure
                reason = _model_budget_reason(cause)
                if reason is None:
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

    async def _finish(self, task_id: str, state: str, reason: str,
                      *, model_provider_error: dict | None = None) -> None:
        try:
            await self._set_state(task_id, state, reason, model_provider_error=model_provider_error)
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

    # Projects
    async def _wait_for_project(self, task_id: str, state: str, turn: int = 0) -> None:
        if not self._project_checkpoints.available() or not await project_work_blocked(task_id):
            return
        had_sandbox = self._sandbox_up
        await self._release_sandbox(task_id, turn)
        await self._set_state(task_id, "PAUSED", "项目已暂停或归档")
        await wait_for_project_resume(task_id)
        await self._set_state(task_id, state)
        if state == "AWAITING_REVIEW":
            return
        if had_sandbox:
            await self._restore_sandbox(task_id, turn)
        else:
            await self._acquire_slot(task_id)

    async def _check_budget(self, task_id: str, turn: int) -> None:
        reason = await workflow.execute_activity("check_budget", StepInput(task_id, turn), **_QUICK)
        if reason:
            await self._turn_spend.wait()
            await self._set_state(task_id, "FAILED", f"budget exceeded: {reason}")
            raise ApplicationError(reason, type="BudgetExceeded", non_retryable=True)

    async def _run(self, task_id: str) -> str:
        spec: dict = await workflow.execute_activity("load_task", task_id, **_QUICK)
        self._task_spec = spec
        if spec.get("_user_interjection"):
            self._interjections.enable()
        spec.setdefault("_kind", "analysis" if "question" in spec else "coding")
        max_fixes: int = spec["max_fix_attempts"]
        turn_timeout = timedelta(seconds=spec["turn_timeout_s"] + 60)

        self._admission = spec.get("_admission", False)
        self._project_checkpoints = ProjectCheckpoints(spec)
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
        await self._wait_for_project(task_id, "PROVISIONING")
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
        if self._interjections.enabled:
            prompt = with_guidance(prompt, extension.get("guidance", []))
        initial_prompt = prompt
        turn, fixes, session_id = 0, 0, None
        skip_turn = extension.get("skip_turn", False)
        unresolved = extension.get("unresolved", [])
        while True:
            await self._wait_for_project(task_id, "RUNNING", turn)
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
                messages = await self._interjections.deliver(task_id, turn)
                interrupted = False
                try:
                    turn_result = await self._run_agent_turn(
                        task_id, spec, turn, prompt, session_id, messages,
                    )
                    session_id = turn_result.session_id
                except TurnInterrupted:
                    interrupted = True
                except ActivityError as exc:
                    await self._turn_spend.wait()
                    if _model_budget_reason(exc.cause) is None and not patched_provider_account_error(exc):
                        await self._check_budget(task_id, turn)
                    raise
                await workflow.execute_activity(
                    hooks["checkpoint"],
                    StepInput(task_id, turn),
                    result_type=CheckpointResult,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=_retry(3),
                )
                if interrupted:
                    continue
                if messages and not turn_result.skipped:
                    await self._interjections.apply(task_id, messages)
            skip_turn = False
            await self._turn_spend.wait()
            await self._wait_for_project(task_id, "RUNNING", turn)

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

            if self._interjections.pending:
                prompt = CONTINUE_WITH_GUIDANCE if session_id else initial_prompt
                continue

            await self._wait_for_project(task_id, "VERIFYING", turn)
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
                if self._interjections.pending:
                    prompt = CONTINUE_WITH_GUIDANCE if session_id else initial_prompt
                    continue
                await self._wait_for_project(task_id, "VERIFYING", turn)

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
                await self._wait_for_project(task_id, "VERIFYING", turn)
                await workflow.execute_activity(
                    "run_acceptance", task_id,
                    start_to_close_timeout=turn_timeout + timedelta(minutes=10),
                    heartbeat_timeout=timedelta(minutes=5), retry_policy=_retry(3),
                )
            # The slot follows the sandbox: a prompt review keeps both, the idle timer below
            # releases both (release_sandbox frees the slot).
            await self._wait_for_project(task_id, "VERIFYING", turn)
            await self._set_state(task_id, "AWAITING_REVIEW")
            if spec.get("auto_finish"):
                state = "SUCCEEDED" if passed and not unresolved else "FAILED"
                if await self._complete(
                    task_id, state, None if state == "SUCCEEDED" else "verification failed",
                ):
                    return state
                prompt = CONTINUE_WITH_GUIDANCE if session_id else initial_prompt
                continue
            try:
                await workflow.wait_condition(self._review_ready, timeout=release_after)
            except TimeoutError:
                # Nobody is looking: stop holding a sandbox. It is rebuilt if work continues.
                await self._release_sandbox(task_id, turn)
                await workflow.wait_condition(self._review_ready)
            await self._wait_for_project(task_id, "AWAITING_REVIEW", turn)
            decision, feedback = self._next_review()

            if decision == "approve" and await self._complete(task_id, "SUCCEEDED"):
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
            if self._interjections.pending and session_id is None:
                prompt += "\n\n" + initial_prompt
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
            self._extension_name = name
            if self._interjections.enabled and self._route_interjections(name):
                for message_id, mode in self._interjections.notices():
                    await self._forward_interjection(message_id, mode)
            result = await self._active_extension
            if self._interjections.enabled:
                self._interjections.acknowledge(result.get("applied_messages", []))
            return result
        finally:
            self._active_extension = None
            self._extension_name = None

    # Task interjection

    @workflow.signal(name=USER_MESSAGE_SIGNAL)
    async def user_message(self, message_id: str, mode: MessageMode) -> None:
        if self._interjections.notify(message_id, mode) and self._route_interjections(self._extension_name):
            await self._forward_interjection(message_id, mode)

    def _route_interjections(self, name: str | None) -> bool:
        return False

    async def _forward_interjection(self, message_id: str, mode: MessageMode) -> None:
        child = self._active_extension
        if child is not None and not child.done():
            try:
                await child.signal(USER_MESSAGE_SIGNAL, args=[message_id, mode])
            except ApplicationError:
                # A child that finished before the signal can no longer take the message.
                # The parent still owns it until the child reports that it was applied.
                workflow.logger.info("Could not forward guidance to extension %s", child.id)

    async def _run_agent_turn(
        self, task_id: str, spec: dict, turn: int, prompt: str, session: str | None,
        messages: list[Guidance],
    ) -> TurnResult:
        prompt = with_guidance(prompt, [message.text for message in messages])
        deferred = self._defer_turn_spend(spec)
        return await self._run_turn_activity(
            "run_agent_turn",
            TurnInput(task_id, turn, prompt, session, self._interjections.enabled,
                      self._interjections.turn_guidance(messages), defer_spend=deferred),
            SettleTurnSpend(task_id, turn) if deferred else None,
            result_type=TurnResult,
            start_to_close_timeout=timedelta(seconds=spec["turn_timeout_s"] + 60),
            heartbeat_timeout=timedelta(seconds=spec.get("turn_heartbeat_timeout_s", 120)),
            retry_policy=_retry(2),
        )

    # Interrupted turn spend
    async def _run_turn_activity(
        self, name: str, inp: Any, settlement: SettleTurnSpend | None, **options: Any,
    ) -> Any:
        try:
            return await self._interjections.run_activity(name, inp, **options)
        except (TurnInterrupted, ActivityError, asyncio.CancelledError):
            if settlement is not None:
                self._turn_spend.start(settlement)
            raise

    def _defer_turn_spend(self, spec: dict) -> bool:
        return (self._interjections.enabled and bool(spec.get("_deferred_turn_spend"))
                and workflow.patched("interrupted-turn-spend"))

    def _review_ready(self) -> bool:
        return bool(self._reviews) or self._interjections.pending

    def _next_review(self) -> tuple[str, str | None]:
        if self._reviews and self._reviews[0][0] == "reject":
            decision = self._reviews[0]
        elif self._interjections.pending:
            decision = ("request_changes", None)
        else:
            decision = self._reviews[0]
        self._reviews.clear()
        return decision

    async def _complete(self, task_id: str, state: str, reason: str | None = None) -> bool:
        if self._interjections.pending:
            return False
        if await self._set_state(task_id, state, reason, wait_for_messages=self._interjections.enabled):
            return True
        self._interjections.request_delivery()
        return False
