"""Durable, bounded evaluation scheduling; all I/O lives in activities."""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, is_cancelled_exception

from sca.eval.errors import GUIDANCE_ABORT, GUIDANCE_CANCELLED_SIGNAL, eval_failure_reason
from sca.orchestration.provider_errors import patched_provider_account_error


@workflow.defn
class EvalRunWorkflow:
    def __init__(self) -> None:
        self.skip_requested = False
        self.guidance_abort = False

    @workflow.signal
    def skip_evolution_candidate(self) -> None:
        if workflow.patched('eval-skip-evolution-candidate'):
            self.skip_requested = True

    @workflow.signal
    def abort_for_guidance(self) -> None:
        if workflow.patched('eval-abort-for-guidance'):
            self.guidance_abort = True

    @workflow.run
    async def run(self, request: dict) -> str:
        options = {"start_to_close_timeout": timedelta(minutes=1)}
        finish = {"id": request["id"]}
        try:
            status, reason = await self.evaluate(request, options)
        except ActivityError as exc:
            if provider := patched_provider_account_error(exc):
                reason = provider.reason
            else:
                reason = eval_failure_reason(exc)
                if reason is None or not workflow.patched("eval-suite-change-nonretryable"):
                    raise
            # Submitted tasks have their own workflows and can finish independently.
            status = "failed"
        if reason is not None:
            finish["reason"] = reason
        await workflow.execute_activity("eval_finish", {**finish, "status": status}, **options)
        return status

    async def evaluate(self, request: dict, options: dict) -> tuple[str, str | None]:
        # Preparing uploads the cases' repositories and may take a while. The other steps are a
        # few API calls: a short timeout means that if a worker dies mid-call, another one retries
        # within a minute instead of after the full timeout (activities do not heartbeat).
        plan = await workflow.execute_activity(
            "eval_prepare", request, start_to_close_timeout=timedelta(minutes=10)
        )
        collect_options = {"report_outcome": True} if workflow.patched("eval-errored-cases") else {}
        spent = 0
        collected = errored = 0
        pending = [(case, repeat) for repeat in range(request["repeat"]) for case in plan["cases"]]
        case_budget = round(request["case_budget"] * 1e6)
        budget = round(request["budget"] * 1e6)
        candidate_run = bool(request.get('evolution_step', {}).get('candidate_id'))
        abortable = bool(request.get('guidance_abortable')) and workflow.patched('eval-guidance-case-cancellation')
        while pending and not (candidate_run and self.skip_requested) and not (abortable and self.guidance_abort):
            batch = []
            deferred = []
            reserved = 0
            for index, (case, repeat) in enumerate(pending):
                if len(batch) == request["concurrency"]:
                    deferred.extend(pending[index:])
                    break
                if not case.get("model_unpriced"):
                    if spent + reserved + case_budget > budget:
                        deferred.append((case, repeat))
                        continue
                    reserved += case_budget
                batch.append({**request, "case": case, "iteration": repeat})
            if not batch:
                break
            pending = deferred
            tasks = await asyncio.gather(*[
                workflow.execute_activity("eval_submit", item, **options) for item in batch])
            active = dict(zip(tasks, batch, strict=True))
            cancelled = False
            while active:
                if abortable and self.guidance_abort and not cancelled:
                    # All regular activity slots may be occupied by the cases being cancelled.
                    await workflow.execute_local_activity('eval_cancel_cases', list(active), **options)
                    cancelled = True
                    if request.get("defer_guidance_spend") and workflow.patched("eval-deferred-guidance-spend"):
                        await self.report_cancelled_cases(request, spent, case_budget, active)
                for task_id, item in list(active.items()):
                    if abortable and self.guidance_abort and not cancelled:
                        break  # Wake at once to cancel the rest of the batch.
                    collect = {**item, "task_id": task_id, **collect_options,
                               **({'guidance_abort': True} if cancelled else {})}
                    result = await self.collect_case(collect, options, interruptible=abortable and not cancelled)
                    if result is not None:
                        if isinstance(result, dict):
                            errored += result["errored"]
                            result = result["cost_usd"]
                        spent += round(result * 1e6)
                        collected += 1
                        del active[task_id]
                if active:
                    if abortable and not cancelled:
                        try:
                            await workflow.wait_condition(lambda: self.guidance_abort, timeout=2)
                        except TimeoutError:
                            pass
                    else:
                        await workflow.sleep(1 if cancelled else 2)
        if abortable and self.guidance_abort:
            return 'cancelled', GUIDANCE_ABORT
        if candidate_run and self.skip_requested:
            return 'cancelled', '操作者已跳过候选'
        if errored * 2 > collected:
            return "failed", f"评测执行错误（{errored}/{collected} 个已执行用例），请查看任务日志"
        return ("budget_exhausted" if pending else "completed"), None

    async def report_cancelled_cases(
        self, request: dict, spent: int, case_budget: int, active: dict[str, dict],
    ) -> None:
        parent = workflow.info().parent
        if parent is None:
            return
        for item in active.values():
            spec = item["case"].get("spec", {})
            if spec.get("subtasks") or spec.get("plan") == "auto":
                # Parallel subtasks can each use the case's remaining budget.
                return
        bound = spent + case_budget * sum(not item["case"].get("model_unpriced") for item in active.values())
        await workflow.get_external_workflow_handle(parent.workflow_id, run_id=parent.run_id).signal(
            GUIDANCE_CANCELLED_SIGNAL, args=[request["id"], bound],
        )

    async def collect_case(self, collect: dict, options: dict, *, interruptible: bool) -> dict | float | None:
        if not interruptible:
            return await workflow.execute_activity("eval_collect", collect, **options)
        result = workflow.start_activity("eval_collect", collect, **options)
        await workflow.wait_condition(lambda: self.guidance_abort or result.done())
        if not result.done():
            # Collection is idempotent. Cancel the request now and collect again after the cases stop.
            result.cancel()
        try:
            return await result
        except (ActivityError, asyncio.CancelledError) as exc:
            if not self.guidance_abort or not is_cancelled_exception(exc):
                raise
            return None
