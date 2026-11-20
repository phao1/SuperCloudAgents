from uuid import uuid4

import pytest
from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, RetryState
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

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
        assert patch_id == 'eval-suite-change-nonretryable'
        return patched

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
