"""Exercise deterministic controller paths without a running Temporal service."""

from uuid import uuid4

import pytest
from temporalio.exceptions import ActivityError, RetryState

from sca.core.model_provider import ModelProviderError
from sca.evolve.models import EvolutionConfig
from sca.evolve.workflow import EvolutionWorkflow
from sca.orchestration.provider_errors import account_failure


@pytest.mark.parametrize('mode,expected,promotions', [
    ('promotion', 'completed', 1), ('rejection', 'completed', 0),
    ('budget', 'budget_exhausted', 0), ('patience', 'patience_exhausted', 0),
    ('approve', 'completed', 1), ('reject', 'completed', 0),
    ('stop', 'stopped', 0), ('invalid', 'patience_exhausted', 0),
    ('eval_failed', 'failed', 0), ('legacy_eval_failed', 'budget_exhausted', 0),
    ('provider_eval_failed', 'failed', 0), ('provider_propose_failed', 'failed', 0),
])
async def test_controller(monkeypatch, mode, expected, promotions):
    from sca.evolve import workflow as module

    controller = EvolutionWorkflow()
    provider = ModelProviderError('billing', 402)
    requests, candidates, statuses, promoted = {}, {}, [], []
    cfg = EvolutionConfig(suite='s', agent_version='v', repeat=1, candidates=1,
                          generations=3 if mode in ('patience', 'invalid') else 1,
                          patience=2, budget_usd=.1 if mode == 'budget' else 10,
                          require_approval=mode in ('approve', 'reject', 'stop')).model_dump()

    async def execute(name, request, **kwargs):
        match name:
            case 'evolve_initialize':
                return {'id': None, 'candidate_id': None, 'overlay': {}}
            case 'evolve_prepare_run':
                requests[request['run_id']] = request
                return {'id': request['run_id']}
            case 'evolve_read_run':
                item = requests[request['run_id']]
                if mode in ('eval_failed', 'legacy_eval_failed'):
                    return {'id': request['run_id'], 'status': 'failed', 'reason': 'Suite changed',
                            'results': []}
                if mode == 'provider_eval_failed':
                    return {'id': request['run_id'], 'status': 'failed', 'reason': provider.reason,
                            'results': [{'infrastructure_error': provider.payload(), 'cost_usd': .01, 'turns': 1}]}
                passed = bool(item['overlay'])
                if mode in ('rejection', 'patience') and item['split'] == 'holdout':
                    passed = not bool(item['overlay'])
                return {'id': request['run_id'], 'suite_hash': 'h', 'status': 'completed',
                        'results': [{'case_id': 'c', 'repeat': 0, 'acceptance_passed': passed,
                                     'verification_passed': True, 'trajectory_passed': True,
                                     'cost_usd': .1, 'turns': 1}]}
            case 'evolve_propose':
                if mode == 'provider_propose_failed':
                    raise ActivityError('Activity failed', scheduled_event_id=1, started_event_id=2,
                                        identity='test', activity_type=name, activity_id='1',
                                        retry_state=RetryState.NON_RETRYABLE_FAILURE) from account_failure(provider)
                return [{'hypothesis': 'h', 'overlay': {'tool_guidance': 'g'},
                         'overlay_sha256': 'h', 'diagnosis': {},
                         'static_check': {'valid': mode != 'invalid'}}]
            case 'evolve_record':
                candidates[request['candidate']['id']] = dict(request['candidate'])
            case 'evolve_status':
                statuses.append(request)
            case 'evolve_promote':
                promoted.append(request)
                return {'id': request['champion_id'], 'candidate_id': request['candidate']['id'],
                        'overlay': request['candidate']['overlay']}
            case _:
                raise AssertionError(name)

    async def child(*args, **kwargs):
        return 'completed'

    async def wait(predicate):
        controller.approve('old-candidate')
        assert controller.approval is None
        if mode == 'stop':
            controller.stop()
        elif mode == 'approve':
            controller.approve(controller.waiting_candidate)
        else:
            controller.reject(controller.waiting_candidate)
        assert predicate()

    monkeypatch.setattr(module.workflow, 'execute_activity', execute)
    monkeypatch.setattr(module.workflow, 'execute_child_workflow', child)
    monkeypatch.setattr(module.workflow, 'patched', lambda _: mode != 'legacy_eval_failed')
    monkeypatch.setattr(module.workflow, 'uuid4', uuid4)
    monkeypatch.setattr(module.workflow, 'wait_condition', wait)
    result = await controller.run({'id': str(uuid4()), 'config': cfg})
    assert result == expected
    assert len(promoted) == promotions
    assert all(c['decision'] != 'pending' for c in candidates.values())
    assert statuses[-1]['status'] == expected
    assert statuses[-1]['spent_usd'] <= cfg['budget_usd']
    if mode == 'eval_failed':
        run_id = next(iter(requests))
        assert statuses[-1]['reason'] == f'评测 {run_id} 失败：Suite changed'
        assert len(statuses) == 1
    if mode == 'provider_eval_failed':
        run_id = next(iter(requests))
        assert statuses[-1]['reason'] == f'评测 {run_id} 失败：{provider.reason}'
        assert statuses[-1]['spent_usd'] == .01
    if mode == 'provider_propose_failed':
        assert statuses[-1]['reason'] == provider.reason
        assert len(requests) == 1 and not candidates
    if mode in ('patience', 'invalid'):
        assert len(candidates) == 2
        assert sum(r['split'] == 'dev' and not r['overlay'] for r in requests.values()) == 1


async def test_workflow_loads_in_temporal_sandbox():
    from temporalio import workflow
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

    SandboxedWorkflowRunner().prepare_workflow(workflow._Definition.must_from_class(EvolutionWorkflow))
