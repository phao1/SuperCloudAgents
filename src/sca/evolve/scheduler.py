"""Periodically evaluate the active champion and start a search when warranted."""

import asyncio
from datetime import datetime, timedelta
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ChildWorkflowError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from temporalio.client import (
        Client,
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleIntervalSpec,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
    )

    from sca.config import Settings
    from sca.db.repo import Store
    from sca.eval.suites import load_suite, select_cases
    from sca.eval.trajectory import overall_passed
    from sca.evolve.champions import PLATFORM_TENANT
    from sca.evolve.models import EvolutionConfig

SCHEDULER_ID = "sca-evolution-scheduler"


def schedule_workflow_id(schedule_id: str | UUID) -> str:
    return f"evolution-schedule-{schedule_id}"


def should_evolve(current: dict, previous: dict | None, *, threshold: float, always: bool) -> bool:
    if current["status"] != "completed" or not current["results"]:
        return False
    outcomes = {(row["case_id"], row["repeat"]): overall_passed(row) for row in current["results"]}
    if always or sum(outcomes.values()) / len(outcomes) < threshold:
        return True
    return bool(previous and previous["status"] == "completed" and any(
        overall_passed(row) and outcomes.get((row["case_id"], row["repeat"])) is False
        for row in previous["results"]
    ))


async def scheduler_activity(name: str, payload: dict) -> dict | list | None:
    return await workflow.execute_activity(
        name, payload, start_to_close_timeout=timedelta(minutes=5),
        retry_policy=RetryPolicy(maximum_attempts=3),
    )


@workflow.defn
class EvolutionSchedulerWorkflow:
    @workflow.run
    async def run(self, request: dict | None = None) -> None:
        now = workflow.now().isoformat()
        if request is None:
            due = await scheduler_activity("evolution_schedules_due", {"now": now})
            for schedule_id in due:
                try:
                    await workflow.start_child_workflow(
                        "EvolutionSchedulerWorkflow", {"schedule_id": schedule_id},
                        id=schedule_workflow_id(schedule_id),
                        parent_close_policy=workflow.ParentClosePolicy.ABANDON,
                    )
                except WorkflowAlreadyStartedError:
                    continue
            return

        context = await scheduler_activity("evolution_schedule_begin", {**request, "now": now})
        if context is None:
            return
        run_id = str(workflow.uuid4())
        eval_request = await scheduler_activity("evolve_prepare_run", {
            **context, "run_id": run_id, "split": "dev", "remaining": context["config"]["budget_usd"],
        })
        outcome = {**context, "run_id": run_id, "evolution_id": str(workflow.uuid4())}
        try:
            status = await workflow.execute_child_workflow("EvalRunWorkflow", eval_request, id=f"eval-{run_id}")
        except ChildWorkflowError:
            await scheduler_activity("evolution_schedule_finish", {**outcome, "status": "failed"})
            raise
        evolution = await scheduler_activity("evolution_schedule_finish", {**outcome, "status": status})
        if evolution is not None:
            await workflow.start_child_workflow(
                "EvolutionWorkflow", evolution, id=f"evolve-{evolution['id']}",
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )


class EvolutionSchedulerActivities:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store.for_tenant(PLATFORM_TENANT)

    def all(self) -> list:
        return [self.due, self.begin, self.finish]

    @activity.defn(name="evolution_schedules_due")
    async def due(self, request: dict) -> list[str]:
        ids = await self.store.due_evolution_schedules(datetime.fromisoformat(request["now"]))
        return [str(schedule_id) for schedule_id in ids]

    @activity.defn(name="evolution_schedule_begin")
    async def begin(self, request: dict) -> dict | None:
        schedule = await self.store.claim_evolution_schedule(
            UUID(request["schedule_id"]), datetime.fromisoformat(request["now"]),
            request.get("force", False),
        )
        if schedule is None:
            return None
        root = self.settings.eval_suites_dir.resolve()
        path = root / schedule["suite"]
        if path.is_symlink() or path.resolve().parent != root:
            raise ValueError("Scheduled suite is outside the eval suite directory")
        suite = await asyncio.to_thread(load_suite, path)
        select_cases(suite, "dev")
        select_cases(suite, "holdout")
        config = EvolutionConfig(
            **schedule["config"], suite=str(path), agent_version=schedule["agent_version"],
            model=schedule["model"],
        )
        champion = await self.store.get_active_champion(config.agent_version, config.model)
        return {
            "schedule_id": request["schedule_id"], "tenant_id": PLATFORM_TENANT,
            "created_by": f"schedule:{request['schedule_id']}", "config": config.model_dump(),
            "suite_hash": suite["hash"], "overlay": champion["overlay"] if champion else {},
            "champion_id": str(champion["id"]) if champion else None,
            "previous_run_id": str(schedule["last_eval_run_id"]) if schedule["last_eval_run_id"] else None,
            "threshold": schedule["threshold"], "always": schedule["always"],
            "auto_rollout_percent": schedule["auto_rollout_percent"],
        }

    @activity.defn(name="evolution_schedule_finish")
    async def finish(self, request: dict) -> dict | None:
        run_id = request["run_id"]
        evolution = None
        if request["status"] == "failed":
            await self.store.finish_eval_run(run_id, "failed")
        if request["status"] == "completed":
            current = await self.store.get_eval_run(run_id)
            previous = await self.previous_run(request)
            if should_evolve(current, previous, threshold=request["threshold"], always=request["always"]):
                evolution = {
                    "id": UUID(request["evolution_id"]), "config": request["config"],
                    "created_by": request["created_by"], "status": "queued",
                }
        recorded = await self.store.record_evolution_schedule_run(
            UUID(request["schedule_id"]), UUID(run_id), evolution,
        )
        if recorded and evolution is not None:
            return {
                "id": request["evolution_id"], "tenant_id": PLATFORM_TENANT,
                "suite_hash": request["suite_hash"], "config": request["config"],
                "created_by": request["created_by"],
                "auto_rollout_percent": request["auto_rollout_percent"],
            }
        return None

    async def previous_run(self, request: dict) -> dict | None:
        if request["previous_run_id"] is None:
            return None
        previous = await self.store.get_eval_run(request["previous_run_id"])
        params, config = previous["variant"], request["config"]
        variant = params.get("variant", {})
        if params.get("suite") == config["suite"] and all(
            variant.get(key) == config[key] for key in ("agent_version", "model")
        ):
            return previous
        return None


async def ensure_evolution_scheduler(client: Client, settings: Settings) -> None:
    try:
        await client.create_schedule(
            SCHEDULER_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    EvolutionSchedulerWorkflow.run, id=SCHEDULER_ID, task_queue=settings.task_queue,
                ),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(minutes=5))]),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
            ),
        )
    except ScheduleAlreadyRunningError:
        pass
