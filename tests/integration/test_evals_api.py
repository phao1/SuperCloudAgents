from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.api.evals import suite_path
from sca.core.model_provider import ModelProviderError
from sca.core.models import TaskState
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


class FakeTemporal:
    def __init__(self):
        self.starts = []
        self.down = False

    async def start_workflow(self, workflow, payload, **kwargs):
        if self.down:
            raise OSError("temporal unreachable")
        self.starts.append((workflow, payload, kwargs))


@pytest.fixture
async def eval_api(store, api_store, tmp_path):
    # Production needs the same narrow grant in a lead-owned migration.
    async with store.sessionmaker.begin() as session:
        await session.execute(text("GRANT INSERT ON eval_runs TO sca_api"))
        await session.execute(text("GRANT UPDATE (status, finished_at) ON eval_runs TO sca_api"))
    temporal = FakeTemporal()
    app = create_app(store=api_store, orchestrator=SimpleNamespace(client=temporal),
                     object_store=LocalObjectStore(tmp_path),
                     authenticator=StaticTokenAuthenticator(
                         {"eval-token": "eval:member", "other": "other", "viewer": "eval:viewer"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                headers={"Authorization": "Bearer eval-token"}) as client:
        yield client, temporal


async def test_suites_and_start(eval_api):
    client, temporal = eval_api
    suites = (await client.get('/v1/evals/suites')).json()
    assert any(s['name'] == 'coding-basic' and len(s['cases']) == 12 for s in suites)
    body = {"suite": "coding-basic", "variant": {"agent_backend": "opencode",
            "agent_version": "2.3.0"}, "budget": 1, "case_budget": .05}
    r = await client.post('/v1/evals/runs', json=body)
    assert r.status_code == 201, r.text
    detail = await client.get('/v1/evals/runs/' + r.json()['id'])
    assert detail.status_code == 200, detail.text
    assert detail.json()['status'] == 'queued'
    assert detail.json()['results'] == []
    _, payload, kwargs = temporal.starts[0]
    assert kwargs['id'] == 'eval-' + r.json()['id']
    assert payload['suite'] == str(suite_path('coding-basic'))
    assert payload['variant']['agent_version'] == '2.3.0'
    assert payload['repeat'] == payload['concurrency'] == 1
    for suite in ('../coding-basic', '/tmp', 'missing', 'coding-basic/..'):
        r = await client.post('/v1/evals/runs', json={**body, 'suite': suite})
        assert r.status_code in (404, 422)
    assert (await client.post('/v1/evals/runs', json={**body, 'budget': -1})).status_code == 422
    for token in ('other', 'viewer'):
        assert (await client.post('/v1/evals/runs', json=body,
                                 headers={'Authorization': 'Bearer '+token})).status_code == 403
    assert (await client.get('/v1/evals/suites', headers={'Authorization': ''})).status_code == 401
    assert (await client.get('/v1/evals/runs', headers={'Authorization': 'Bearer other'})).status_code == 403


async def test_detail_list_compare(eval_api, store):
    client, _ = eval_api
    task, _ = await store.for_tenant('eval').create_task(tenant_id='eval', kind='coding', spec={
        'repo_url': '/tmp/repo', 'instruction': 'fix'}, idempotency_key=None)
    ids = [uuid4(), uuid4()]
    for run_id, passed in zip(ids, (False, True), strict=True):
        await store.create_eval_run({'id': run_id, 'tenant_id': 'eval', 'suite': 'coding-basic',
                                    'suite_hash': 'same', 'variant': {},
                                    'created_by': 'test', 'status': 'completed'})
        await store.record_eval_result({'run_id': run_id, 'tenant_id': 'eval', 'case_id': 'case',
            'repeat': 0, 'task_id': UUID(str(task.id)), 'acceptance_passed': passed,
            'verification_passed': True, 'turns': 1, 'tokens': 10, 'cost_usd': .01, 'duration_s': 2})
    detail = await client.get('/v1/evals/runs/'+str(ids[0]))
    assert detail.status_code == 200, detail.text
    assert detail.json()['results'][0]['task_id'] == str(task.id)
    assert detail.json()['failed'] == 1
    listing = (await client.get('/v1/evals/runs')).json()
    assert len(listing) == 2
    assert float(listing[0]['cost_usd']) == .01
    report = (await client.get(f'/v1/evals/compare?a={ids[0]}&b={ids[1]}')).json()
    assert report['B_wins'] == 1 and report['A_wins'] == 0
    assert (await client.get('/v1/evals/runs/'+str(uuid4()))).status_code == 404
    mismatch = uuid4()
    await store.create_eval_run({'id': mismatch, 'tenant_id': 'eval', 'suite': 'other',
                                'suite_hash': 'different', 'variant': {},
                                'created_by': 'test', 'status': 'completed'})
    assert (await client.get(
        f'/v1/evals/compare?a={ids[0]}&b={mismatch}')).status_code == 422
    await store.set_state(task.id, TaskState.CANCELLED)


async def test_failed_provider_case_is_excluded_from_evaluation_scores(eval_api, store):
    client, _ = eval_api
    provider = ModelProviderError('billing', 402)
    task, _ = await store.create_task(tenant_id='eval', kind='coding', spec={}, idempotency_key=None)
    await store.set_state(task.id, TaskState.FAILED, provider.reason, model_provider_error=provider.payload())
    run_id = uuid4()
    await store.create_eval_run({'id': run_id, 'tenant_id': 'eval', 'suite': 'coding-basic',
                                'suite_hash': 'same', 'variant': {},
                                'created_by': 'test', 'status': 'running'})
    await store.record_eval_result({'run_id': run_id, 'tenant_id': 'eval', 'case_id': 'case',
        'repeat': 0, 'task_id': task.id, 'acceptance_passed': None, 'verification_passed': None,
        'turns': 1, 'tokens': 0, 'cost_usd': 0, 'duration_s': 1})
    await store.finish_eval_run(str(run_id), 'failed', reason=provider.reason)
    response = await client.get(f'/v1/evals/runs/{run_id}')
    assert response.status_code == 200
    run = response.json()
    assert run['status'] == 'failed' and run['reason'] == provider.reason
    assert (run['passed'], run['failed'], run['infrastructure_errors']) == (0, 0, 1)
    (case,) = run['results']
    assert case['infrastructure_error'] == provider.payload() and case['passed'] is None
    report = (await client.get(f'/v1/evals/compare?a={run_id}&b={run_id}')).json()
    assert report['A'] == report['B'] == {}
    assert report['infrastructure_errors'] == {'A': 1, 'B': 1}


def test_suite_path_rejects_symlinks(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from sca.api import evals

    root = tmp_path / 'suites'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'escape').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(evals, 'get_settings', lambda: SimpleNamespace(eval_suites_dir=root))
    with pytest.raises(HTTPException) as exc:
        suite_path('escape')
    assert exc.value.status_code == 404


async def test_failed_workflow_start_fails_the_run(eval_api):
    client, temporal = eval_api
    temporal.down = True
    body = {"suite": "coding-basic", "variant": {"agent_backend": "opencode"}, "budget": 1,
            "case_budget": .05}
    assert (await client.post('/v1/evals/runs', json=body)).status_code == 503
    runs = (await client.get('/v1/evals/runs')).json()
    assert runs and runs[0]['status'] == 'failed'
