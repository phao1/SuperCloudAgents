"""Scheduled evaluation and evolution through fake Temporal and Store boundaries."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from temporalio import workflow
from temporalio.client import ScheduleAlreadyRunningError, ScheduleOverlapPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from sca.config import Settings
from sca.evolve.activities import EvolutionActivities
from sca.evolve.scheduler import (
    SCHEDULER_ID,
    EvolutionSchedulerActivities,
    EvolutionSchedulerWorkflow,
    ensure_evolution_scheduler,
    schedule_workflow_id,
    should_evolve,
)


def run(cases, status="completed"):
    return {"status": status, "results": [
        {"case_id": name, "repeat": repeat, "acceptance_passed": passed}
        for name, repeat, passed in cases
    ]}


@pytest.mark.parametrize("current,previous,threshold,always,expected", [
    (run([("a", 0, False)]), None, .8, False, True),
    (run([("a", 0, True), ("b", 0, False)]), None, .5, False, False),
    (run([("a", 0, False), ("b", 0, True)]), run([("a", 0, True), ("b", 0, False)]), .5, False, True),
    (run([("a", 1, False)]), run([("a", 0, True)]), 0, False, False),
    (run([("a", 0, False)]), run([("a", 0, True)], "budget_exhausted"), 0, False, False),
    (run([("a", 0, True)]), None, 0, True, True),
    (run([("a", 0, False)], "budget_exhausted"), None, 1, True, False),
    (run([]), None, 1, True, False),
])
def test_scheduler_decisions(current, previous, threshold, always, expected):
    assert should_evolve(current, previous, threshold=threshold, always=always) is expected


async def test_native_schedule_registration_and_workflow_sandbox():
    settings = Settings(_env_file=None, task_queue="local")
    client = SimpleNamespace(create_schedule=AsyncMock())
    await ensure_evolution_scheduler(client, settings)
    schedule_id, schedule = client.create_schedule.call_args.args
    assert schedule_id == SCHEDULER_ID
    assert schedule.action.workflow == "EvolutionSchedulerWorkflow"
    assert schedule.action.task_queue == "local"
    assert schedule.spec.intervals[0].every == timedelta(minutes=5)
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    client.create_schedule.side_effect = ScheduleAlreadyRunningError()
    await ensure_evolution_scheduler(client, settings)
    SandboxedWorkflowRunner().prepare_workflow(workflow._Definition.must_from_class(
        EvolutionSchedulerWorkflow,
    ))


async def test_scheduler_runs_due_schedules_even_when_one_is_active(monkeypatch):
    ids = [str(uuid4()), str(uuid4())]
    monkeypatch.setattr(workflow, "now", lambda: datetime.now(UTC))
    monkeypatch.setattr(workflow, "execute_activity", AsyncMock(return_value=ids))
    start = AsyncMock(side_effect=[
        WorkflowAlreadyStartedError(schedule_workflow_id(ids[0]), "EvolutionSchedulerWorkflow"), None,
    ])
    monkeypatch.setattr(workflow, "start_child_workflow", start)
    await EvolutionSchedulerWorkflow().run()
    assert [call.kwargs["id"] for call in start.await_args_list] == list(map(schedule_workflow_id, ids))
    assert start.await_args.kwargs["parent_close_policy"] == workflow.ParentClosePolicy.ABANDON


async def test_keyless_demo_does_not_claim_scheduled_evaluations(tmp_path, monkeypatch):
    config = tmp_path / "provider.env"
    config.touch()
    monkeypatch.setenv("SCA_PORTAL_LOCAL_CONFIG", str(config))
    store = SimpleNamespace(due_evolution_schedules=AsyncMock(), claim_evolution_schedule=AsyncMock())
    scheduler = EvolutionSchedulerActivities(Settings(_env_file=None), SimpleNamespace(for_tenant=lambda _: store))
    request = {"now": datetime.now(UTC).isoformat(), "schedule_id": str(uuid4())}
    assert await scheduler.due(request) == []
    assert await scheduler.begin(request) is None
    store.due_evolution_schedules.assert_not_called()
    store.claim_evolution_schedule.assert_not_called()


@pytest.mark.parametrize("status,search", [("completed", True), ("budget_exhausted", False)])
@pytest.mark.parametrize("backend", ["opencode", "dsh"])
async def test_scheduler_evaluates_current_champion_and_records_outcome(
    tmp_path, monkeypatch, status, search, backend,
):
    suite = tmp_path / "suite"
    for split in ("dev", "holdout"):
        case = suite / "cases" / split
        case.mkdir(parents=True)
        (case / "task.yaml").write_text(f"split: {split}\n")
        (case / "acceptance.yaml").write_text("command: 'true'\n")
    (suite / "suite.yaml").write_text("name: suite\n")
    settings = Settings(_env_file=None, eval_suites_dir=tmp_path)
    schedule_id, champion_id = uuid4(), uuid4()
    overlay = {"tool_guidance": "benchmark candidate"}
    claimed = {"suite": "suite", "config": {"budget_usd": 1, "repeat": 1}, "agent_version": "v",
               "model": "m", "last_eval_run_id": None, "threshold": 1.0, "always": False,
               "auto_rollout_percent": 12}
    if backend == "dsh":
        claimed.update(agent_backend="dsh", agent_version=settings.dsh_version)
    store = SimpleNamespace(
        claim_evolution_schedule=AsyncMock(return_value=claimed),
        get_active_champion=AsyncMock(return_value={"id": champion_id, "overlay": overlay}),
        create_eval_run=AsyncMock(), get_eval_run=AsyncMock(return_value=run([("dev", 0, False)])),
        record_evolution_schedule_run=AsyncMock(return_value=True),
    )
    root = SimpleNamespace(for_tenant=lambda tenant: store if tenant == "eval" else None)
    scheduler = EvolutionSchedulerActivities(settings, root)
    evolution = EvolutionActivities(settings, root, None)
    activities = {"evolution_schedule_begin": scheduler.begin, "evolve_prepare_run": evolution.prepare_run,
                  "evolution_schedule_finish": scheduler.finish}

    async def execute(name, payload, **kwargs):
        return await activities[name](payload)

    now = datetime.now(UTC)
    monkeypatch.setattr(workflow, "now", lambda: now)
    monkeypatch.setattr(workflow, "uuid4", uuid4)
    monkeypatch.setattr(workflow, "execute_activity", execute)
    child = AsyncMock(return_value=status)
    start = AsyncMock()
    monkeypatch.setattr(workflow, "execute_child_workflow", child)
    monkeypatch.setattr(workflow, "start_child_workflow", start)
    await EvolutionSchedulerWorkflow().run({"schedule_id": str(schedule_id), "force": True})
    store.claim_evolution_schedule.assert_awaited_once_with(schedule_id, now, True)
    name, payload = child.call_args.args
    assert name == "EvalRunWorkflow"
    assert payload["split"] == "dev"
    assert payload["champion_id"] == str(champion_id)
    assert payload["variant"]["agent_backend"] == backend
    assert payload["variant"]["agent_version"] == claimed["agent_version"]
    store.get_active_champion.assert_awaited_once_with(claimed["agent_version"], "m", agent_backend=backend)
    assert payload["variant"]["agent_overlay"] == overlay
    assert payload["variant"]["use_champion"] is False
    assert store.create_eval_run.call_args.args[0]["variant"] == payload
    assert store.record_evolution_schedule_run.call_args.args[:2] == (schedule_id, UUID(payload["id"]))
    queued = store.record_evolution_schedule_run.call_args.args[2]
    if search:
        assert queued["status"] == "queued"
        name, request = start.call_args.args
        assert name == "EvolutionWorkflow"
        assert request["id"] == str(queued["id"])
        assert request["config"]["agent_backend"] == backend
        assert request["auto_rollout_percent"] == 12
        assert start.call_args.kwargs["parent_close_policy"] == workflow.ParentClosePolicy.ABANDON
    else:
        assert queued is None
        start.assert_not_awaited()


async def test_scheduled_promotion_can_set_zero_percent():
    store = SimpleNamespace(activate_champion=AsyncMock(return_value={"id": uuid4()}),
                            update_evolution=AsyncMock())
    evolution = EvolutionActivities(Settings(_env_file=None), SimpleNamespace(for_tenant=lambda _: store), None)
    request = {"id": str(uuid4()), "tenant_id": "eval", "created_by": "schedule:test",
               "champion_id": str(uuid4()), "parent_champion_id": None,
               "config": {"agent_backend": "dsh", "agent_version": evolution.settings.dsh_version, "model": "m"},
               "auto_rollout_percent": 0,
               "candidate": {"id": str(uuid4()), "overlay": {}, "overlay_sha256": "hash",
                             "dev_metrics": {}, "holdout_metrics": {}}}
    await evolution.promote(request)
    assert store.activate_champion.call_args.kwargs["rollout"] == (0, "schedule:test")
    assert store.activate_champion.call_args.args[1]["agent_backend"] == "dsh"


async def test_schedule_previous_run_must_match_the_backend():
    variant = {"agent_version": "v", "model": "m"}
    previous = {"variant": {"suite": "/suite", "variant": variant}}
    store = SimpleNamespace(get_eval_run=AsyncMock(return_value=previous))
    activities = EvolutionSchedulerActivities(Settings(_env_file=None), SimpleNamespace(for_tenant=lambda _: store))
    request = {"previous_run_id": str(uuid4()), "config": {"suite": "/suite", **variant, "agent_backend": "dsh"}}
    assert await activities.previous_run(request) is None
    variant["agent_backend"] = "dsh"
    assert await activities.previous_run(request) == previous
