"""Evolution API authorization, queueing, approval and rollback using fakes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sca.api.authz import ForbiddenError
from sca.api.evolutions import router
from sca.core.models import Principal


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    from sca.api import evolutions

    suite = tmp_path / 'suite'
    suite.mkdir()
    settings = SimpleNamespace(eval_suites_dir=tmp_path, task_queue='q', dsh_version='0.2.2-rc.5')
    monkeypatch.setattr(evolutions, 'get_settings', lambda: settings)

    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr(evolutions.asyncio, 'to_thread', inline)
    monkeypatch.setattr(evolutions, 'load_suite', lambda path: {
        'hash': 'h', 'cases': [{'spec': {}, 'split': 'dev'}, {'spec': {}, 'split': 'holdout'}]})
    candidate_id = uuid4()
    store = SimpleNamespace(create_evolution=AsyncMock(), update_evolution=AsyncMock(),
                            list_evolutions=AsyncMock(return_value=[]),
                            list_champions=AsyncMock(return_value=[]),
                            get_evolution=AsyncMock(return_value={
                                'status': 'awaiting_approval', 'candidates': [
                                    {'id': candidate_id, 'decision': 'pending', 'reason': 'awaiting approval'}]}),
                            activate_champion=AsyncMock(return_value={'active': True}))
    handle = SimpleNamespace(signal=AsyncMock())
    temporal = SimpleNamespace(start_workflow=AsyncMock(), get_workflow_handle=lambda workflow_id: handle)
    app = FastAPI()
    app.include_router(router)
    app.state.store = SimpleNamespace(for_tenant=lambda tenant: store)
    app.state.orchestrator = SimpleNamespace(client=temporal)
    current = Principal(tenant_id='eval', subject='test', role='admin')
    async def current_principal(token):
        return current

    app.state.authenticator = SimpleNamespace(authenticate=current_principal)

    @app.exception_handler(ForbiddenError)
    async def forbidden(request: Request, exc: ForbiddenError):
        return JSONResponse(status_code=403, content={'detail': str(exc)})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        yield http, store, temporal, handle, current, candidate_id


async def test_start_creates_row_before_workflow(harness):
    http, store, temporal, _, _, _ = harness

    async def start(*args, **kwargs):
        store.create_evolution.assert_awaited_once()
        payload = args[1]
        assert payload['suite_hash'] == 'h'
        assert UUID(payload['id'])
        assert payload['config']['suite'].endswith('/suite')
        assert payload['config']['agent_backend'] == 'opencode'

    temporal.start_workflow.side_effect = start
    response = await http.post('/v1/evolutions', json={'suite': 'suite', 'agent_version': 'v'})
    assert response.status_code == 201
    assert response.json()['status'] == 'queued'
    assert (await http.get('/v1/evolutions')).status_code == 200
    assert (await http.get('/v1/champions')).status_code == 200


@pytest.mark.parametrize('version', [None, '0.2.2-rc.5'])
async def test_dsh_start_pins_installed_npm_version(harness, version):
    http, store, temporal, _, _, _ = harness
    body = {'suite': 'suite', 'agent_backend': 'dsh'}
    if version is not None:
        body['agent_version'] = version
    assert (await http.post('/v1/evolutions', json=body)).status_code == 201
    config = store.create_evolution.call_args.args[0]['config']
    assert config['agent_backend'] == 'dsh' and config['agent_version'] == '0.2.2-rc.5'
    assert temporal.start_workflow.call_args.args[1]['config'] == config
    assert (await http.get('/v1/champions', params={'agent_backend': 'dsh'})).status_code == 200
    store.list_champions.assert_awaited_once_with(None, None, agent_backend='dsh')


@pytest.mark.parametrize('target', [
    {'agent_backend': 'fake', 'agent_version': 'v'},
    {'agent_backend': 'dsh', 'agent_version': '0.1.0'},
    {'agent_backend': 'dsh', 'agent_version': ''},
    {'agent_backend': 'opencode'},
])
async def test_invalid_evolution_target_never_queues(harness, target):
    http, store, temporal, _, _, _ = harness
    assert (await http.post('/v1/evolutions', json={'suite': 'suite', **target})).status_code == 422
    store.create_evolution.assert_not_awaited()
    temporal.start_workflow.assert_not_awaited()


@pytest.mark.parametrize('role,tenant,scopes', [
    ('member', 'eval', []), ('admin', 'other', []), ('admin', 'eval', ['ci:report'])])
async def test_admin_eval_authorization(harness, role, tenant, scopes):
    http, _, _, _, user, _ = harness
    user.role, user.tenant_id, user.scopes = role, tenant, scopes
    assert (await http.get('/v1/evolutions')).status_code == 403
    assert (await http.post('/v1/champions/' + str(uuid4()) + '/activate')).status_code == 403


async def test_approval_is_bound_to_candidate_and_state(harness):
    http, store, _, handle, _, candidate_id = harness
    evolution_id = uuid4()
    path = f'/v1/evolutions/{evolution_id}'
    response = await http.post(path + '/approve', json={'candidate_id': str(candidate_id)})
    assert response.status_code == 200
    handle.signal.assert_awaited_once_with('approve', str(candidate_id))
    assert (await http.post(path + '/reject', json={'candidate_id': str(uuid4())})).status_code == 409
    assert (await http.post(path + '/stop')).status_code == 200
    store.get_evolution.return_value['status'] = 'completed'
    assert (await http.post(path + '/stop')).status_code == 409


async def test_start_failure_and_invalid_suite(harness):
    http, store, temporal, _, _, _ = harness
    temporal.start_workflow.side_effect = RuntimeError('offline')
    assert (await http.post('/v1/evolutions', json={'suite': 'suite', 'agent_version': 'v'})).status_code == 503
    assert store.update_evolution.call_args.args[1]['status'] == 'failed'
    assert (await http.post('/v1/evolutions', json={'suite': '../outside', 'agent_version': 'v'})).status_code == 422
    response = await http.post('/v1/champions/' + str(uuid4()) + '/activate')
    assert response.status_code == 200
    assert response.json()['active']
