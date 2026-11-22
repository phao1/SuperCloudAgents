from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, RetryState
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from sca.api.evals import summary
from sca.config import Settings
from sca.eval.activities import EvalActivities
from sca.eval.compare import compare
from sca.eval.workflow import EvalRunWorkflow


@pytest.mark.parametrize('patched', [True, False])
async def test_eval_submit_failure_finishes_only_on_patched_path(monkeypatch, patched):
    attempts, results, finished = [], [], []

    async def execute(name, request, **options):
        assert 'retry_policy' not in options
        if name == 'eval_prepare':
            return {'cases': [{'id': name} for name in ('first', 'changed', 'pending')]}
        if name == 'eval_submit':
            case_id = request['case']['id']
            attempts.append(case_id)
            if case_id == 'changed':
                raise ActivityError(
                    'Activity failed', scheduled_event_id=1, started_event_id=2,
                    identity='test', activity_type=name, activity_id=case_id,
                    retry_state=RetryState.NON_RETRYABLE_FAILURE,
                ) from ApplicationError('Suite changed', type='SuiteChanged', non_retryable=True)
            return case_id
        if name == 'eval_collect':
            results.append(request['task_id'])
            return .1
        assert name == 'eval_finish'
        finished.append(request)

    def use_patch(patch_id):
        return patch_id != 'eval-suite-change-nonretryable' or patched

    monkeypatch.setattr(workflow, 'execute_activity', execute)
    monkeypatch.setattr(workflow, 'patched', use_patch)
    run_id = str(uuid4())
    request = {'id': run_id, 'repeat': 1, 'concurrency': 1, 'budget': .3, 'case_budget': .1}
    if patched:
        assert await EvalRunWorkflow().run(request) == 'failed'
        assert finished == [{'id': run_id, 'status': 'failed', 'reason': 'Suite changed'}]
    else:
        with pytest.raises(ActivityError):
            await EvalRunWorkflow().run(request)
        assert not finished
    assert attempts == ['first', 'changed']
    assert results == ['first']


async def test_eval_workflow_loads_in_temporal_sandbox():
    SandboxedWorkflowRunner().prepare_workflow(workflow._Definition.must_from_class(EvalRunWorkflow))


@pytest.mark.parametrize("failures,total,expected", [
    (3, 3, "failed"), (2, 3, "failed"), (1, 2, "completed"), (1, 3, "completed"), (0, 2, "completed"),
])
async def test_eval_distinguishes_execution_errors_from_scored_failures(monkeypatch, failures, total, expected):
    task_ids = [str(uuid4()) for _ in range(total)]
    store = SimpleNamespace(record_eval_result=AsyncMock(), finish_eval_run=AsyncMock())
    activities = EvalActivities(Settings(_env_file=None), store)

    def api(request):
        path = request.url.path.split("/")
        task_id = path[3]
        incomplete = task_ids.index(task_id) < failures
        if path[-1] == "events":
            return httpx.Response(200, json=[
                {"seq": 1, "type": "agent.turn_started", "payload": {"turn": 1}},
                {"seq": 2, "type": "agent.error" if incomplete else "agent.turn_completed", "payload": {}},
            ])
        return httpx.Response(200, json={
            "id": task_id, "state": "FAILED", "result": {"acceptance_passed": False},
            "usage": {"cost_micro_usd": 1000}, "created_at": "2026-11-22T00:00:00Z",
            "updated_at": "2026-11-22T00:00:09Z",
        })

    monkeypatch.setattr(activities, "http", lambda: httpx.AsyncClient(
        base_url="http://api", transport=httpx.MockTransport(api),
    ))

    async def execute(name, item, **options):
        match name:
            case "eval_prepare":
                return {"cases": [{"id": index} for index in range(total)]}
            case "eval_submit":
                return task_ids[item["case"]["id"]]
            case "eval_collect":
                return await activities.collect(item)
            case "eval_finish":
                await activities.finish(item)

    monkeypatch.setattr(workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow, "patched", lambda patch_id: patch_id == "eval-errored-cases")
    run_id = str(uuid4())
    assert await EvalRunWorkflow().run({
        "id": run_id, "repeat": 1, "concurrency": 2, "budget": 1, "case_budget": .1,
    }) == expected
    reason = store.finish_eval_run.await_args.kwargs["reason"]
    if expected == "failed":
        assert reason and f"{failures}/{total}" in reason and "评测执行错误" in reason
    else:
        assert reason is None
    rows = [call.args[0] for call in store.record_eval_result.await_args_list]
    run = {"suite_hash": "same", "results": rows}
    errors = summary(run)
    assert (errors["failed"], errors["passed"], errors["errored"]) == (total - failures, 0, failures)
    assert all(row["cost_usd"] == .001 for row in rows)
    assert all(row["trajectory_passed"] is None for row in rows[:failures])
    report = compare(run, run)
    assert report["errored"] == {"A": failures, "B": failures}
    assert all(row["A"] is row["B"] is None for row in report["cases"][:failures])
