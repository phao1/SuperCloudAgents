from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


async def test_eval_persistence_idempotent(store):
    task, _ = await store.create_task(tenant_id='eval', kind='coding', spec={'instruction': 'x'}, idempotency_key=None)
    run_id = uuid4()
    run = {'id': run_id, 'tenant_id': 'eval', 'suite': 'test', 'suite_hash': 'abc',
           'variant': {}, 'created_by': 'test', 'status': 'running'}
    await store.create_eval_run(run)
    await store.create_eval_run(run)
    result = {'run_id': run_id, 'tenant_id': 'eval', 'case_id': 'one', 'repeat': 0,
              'task_id': task.id, 'acceptance_passed': False, 'verification_passed': True,
              'turns': 1, 'tokens': 3, 'cost_usd': .01, 'duration_s': 2}
    await store.record_eval_result(result)
    await store.record_eval_result(result)
    await store.finish_eval_run(str(run_id), 'completed')
    saved = await store.get_eval_run(str(run_id))
    assert saved['status'] == 'completed'
    assert saved['finished_at'] is not None
    assert len(saved['results']) == 1
    assert saved['results'][0]['acceptance_passed'] is False


async def test_acceptance_result_and_event_are_idempotent(store):
    task, _ = await store.create_task(tenant_id='eval', kind='coding', spec={}, idempotency_key=None)
    await store.complete_acceptance(str(task.id), False, 1, 'failed')
    await store.complete_acceptance(str(task.id), True, 0, 'duplicate')
    saved = await store.get_task(task.id)
    assert saved.result['acceptance_passed'] is False
    events = await store.list_events(task.id)
    completed = [event for event in events if event.type == 'acceptance.completed']
    assert len(completed) == 1
    assert completed[0].payload == {'passed': False, 'exit_code': 1, 'output_tail': 'failed'}


async def test_queued_run_prepare_is_idempotent(store):
    from pathlib import Path

    from sca.config import Settings
    from sca.eval.activities import EvalActivities
    from sca.eval.suites import load_suite

    suite = load_suite(Path("evals/coding-basic"))
    run_id = uuid4()
    await store.create_eval_run({
        'id': run_id, 'tenant_id': 'eval', 'suite': suite['name'],
        'suite_hash': suite['hash'], 'variant': {}, 'created_by': 'api', 'status': 'queued',
    })
    activity = EvalActivities(Settings(_env_file=None), store)
    request = {'id': str(run_id), 'suite': str(Path('evals/coding-basic').resolve())}
    await activity.prepare(request)
    await activity.prepare(request)
    saved = await store.get_eval_run(str(run_id))
    assert saved['status'] == 'running'
    assert saved['created_by'] == 'api'
    await store.finish_eval_run(str(run_id), 'completed')
    await activity.prepare(request)
    assert (await store.get_eval_run(str(run_id)))['status'] == 'completed'


async def test_cli_persists_before_start(store, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sca.eval import cli

    started = []

    async def start_workflow(workflow, request, **kwargs):
        saved = await store.get_eval_run(request['id'])
        assert saved['status'] == 'queued'
        started.append(saved)
        return SimpleNamespace(result=AsyncMock())

    monkeypatch.setattr(cli, 'make_engine', lambda url: SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(cli, 'make_sessionmaker', lambda engine: store.sessionmaker)
    monkeypatch.setattr(cli.Client, 'connect', AsyncMock(return_value=SimpleNamespace(
        start_workflow=start_workflow)))
    run_id = await cli.start(Path('evals/coding-basic'), None, None, 1, 1, 1, .05,
                             None, True, False)
    assert str(started[0]['id']) == run_id
