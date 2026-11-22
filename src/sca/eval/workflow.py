"""Durable, bounded evaluation scheduling; all I/O lives in activities."""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError

from sca.eval.errors import eval_failure_reason
from sca.orchestration.provider_errors import patched_provider_account_error


@workflow.defn
class EvalRunWorkflow:
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
        while pending:
            batch = []
            for _ in range(min(request["concurrency"], len(pending))):
                if spent + round(request["case_budget"] * 1e6) * (len(batch) + 1) > round(request["budget"] * 1e6):
                    break
                case, repeat = pending.pop(0)
                batch.append({**request, "case": case, "iteration": repeat})
            if not batch:
                break
            tasks = await asyncio.gather(*[
                workflow.execute_activity("eval_submit", item, **options) for item in batch])
            active = dict(zip(tasks, batch, strict=True))
            while active:
                for task_id, item in list(active.items()):
                    result = await workflow.execute_activity(
                        "eval_collect", {**item, "task_id": task_id, **collect_options}, **options)
                    if result is not None:
                        if isinstance(result, dict):
                            errored += result["errored"]
                            result = result["cost_usd"]
                        spent += round(result * 1e6)
                        collected += 1
                        del active[task_id]
                if active:
                    await workflow.sleep(2)
        if errored * 2 > collected:
            return "failed", f"评测执行错误（{errored}/{collected} 个已执行用例），请查看任务日志"
        return ("budget_exhausted" if pending else "completed"), None
