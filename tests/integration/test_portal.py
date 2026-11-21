from types import SimpleNamespace

import httpx
import pytest

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.core.models import TaskState
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


async def test_portal_and_config(api_store, tmp_path, monkeypatch):
    from sca.api import portal
    from sca.config import Settings

    settings = Settings(_env_file=None, model_gateway_master_key='sentinel-secret',
                        api_tokens='sentinel-token:eval',
                        model_gateway_url='http://user:sentinel-password@localhost:1')
    monkeypatch.setattr(portal, 'get_settings', lambda: settings)

    async def models():
        return True, ['model-one']

    monkeypatch.setattr(portal, 'gateway_models', models)
    app = create_app(store=api_store, orchestrator=SimpleNamespace(),
                     object_store=LocalObjectStore(tmp_path),
                     authenticator=StaticTokenAuthenticator({'token': 'eval'}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
        page = await c.get('/portal')
        assert page.status_code == 200 and '自演进闭环' in page.text
        architecture = await c.get('/portal/architecture')
        assert architecture.status_code == 200 and 'data-node-id="task_entry"' in architecture.text
        task_flow = await c.get('/portal/task-flow.js')
        assert task_flow.status_code == 200 and 'SCAJobFlow' in task_flow.text
        timeline = await c.get('/portal/task-timeline.js')
        assert timeline.status_code == 200 and 'SCATaskTimeline' in timeline.text
        assert (await c.get('/v1/portal/config')).status_code == 401
        r = await c.get('/v1/portal/config', headers={'Authorization': 'Bearer token'})
        assert r.status_code == 200
        assert r.json()['models'] == ['model-one']
        assert r.json()['gateway_reachable'] is True
        assert r.json()['temporal_reachable'] is False
        for secret in ('sentinel-secret', 'sentinel-password', 'sentinel-token', 'http://'):
            assert secret not in r.text


@pytest.mark.parametrize('models_status', [200, 401])
async def test_gateway_probe_authenticated(tmp_path, monkeypatch, models_status):
    from sca.api import portal
    from sca.config import Settings

    settings = Settings(_env_file=None, model_gateway_url='http://gateway',
                        model_gateway_master_key='sentinel-master')
    monkeypatch.setattr(portal, 'get_settings', lambda: settings)
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path == '/health/liveliness':
            return httpx.Response(200, json={'status': 'alive'})
        assert request.headers['Authorization'] == 'Bearer sentinel-master'
        return httpx.Response(models_status, json={
            'data': [{'id': 'model-one', 'secret': 'sentinel-master'}, None, {'id': 42}]})

    original = httpx.AsyncClient
    probe = original(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(portal.httpx, 'AsyncClient', lambda **kwargs: probe)
    app = create_app(orchestrator=SimpleNamespace(),
                     object_store=LocalObjectStore(tmp_path),
                     authenticator=StaticTokenAuthenticator({'token': 'eval'}))
    async with original(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/v1/portal/config', headers={'Authorization': 'Bearer token'})
    assert response.status_code == 200
    assert response.json()['gateway_reachable'] is True
    assert response.json()['models'] == (['model-one'] if models_status == 200 else [])
    assert 'sentinel-master' not in response.text
    assert paths == ['/health/liveliness', '/v1/models']


async def test_local_task_progress_is_read_only_and_tenant_scoped(store, api_store, tmp_path, monkeypatch):
    from sca.api import portal
    from sca.config import Settings

    settings = Settings(_env_file=None, portal_local_task_progress=True)
    monkeypatch.setattr(portal, 'get_settings', lambda: settings)
    eval_store = store.for_tenant('eval')
    task, _ = await eval_store.create_task(
        tenant_id='eval', kind='coding', idempotency_key=None,
        spec={'agent_backend': 'opencode', 'plan': 'auto', 'plan_review': True,
              'instruction': 'sentinel-private-prompt', 'acceptance_files': 'sentinel-private-file'},
    )
    await eval_store.set_state(task.id, TaskState.PROVISIONING, 'sentinel-private-reason')
    await eval_store.append_event(task.id, 'memory.injected', {
        'ids': ['sentinel-private-memory-id'],
        'entries': [{'text': 'sentinel-private-memory'}],
        'method': 'all',
    })
    await eval_store.append_event(task.id, 'agent.command', {
        'subtask': 'validation', 'turn': 1, 'exit_code': 0,
        'command': 'sentinel-private-command', 'output_tail': 'sentinel-private-output',
    })
    await eval_store.append_event(task.id, 'verification.completed', {
        'passed': True, 'checks': [{'passed': True, 'command': 'sentinel-private-check'}],
    })
    other, _ = await store.for_tenant('other').create_task(
        tenant_id='other', kind='coding', spec={}, idempotency_key=None,
    )

    app = create_app(store=api_store, orchestrator=SimpleNamespace(),
                     object_store=LocalObjectStore(tmp_path),
                     authenticator=StaticTokenAuthenticator({'token': 'eval'}))
    transport = httpx.ASGITransport(app=app, client=('127.0.0.1', 1234))
    async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as client:
        list_response = await client.get('/v1/portal/tasks')
        assert list_response.status_code == 200
        assert list_response.headers['cache-control'] == 'no-store'
        assert [row['id'] for row in list_response.json()] == [str(task.id)]

        path = f'/v1/portal/tasks/{task.id}'
        detail = await client.get(path)
        assert detail.status_code == 200
        assert detail.json()['state'] == 'PROVISIONING'
        assert detail.json()['spec']['plan_review'] is True
        assert detail.json()['acceptance_configured'] is True

        events = await client.get(path + '/events', params={'after': 1, 'limit': 4})
        assert events.status_code == 200
        rows = events.json()
        assert [row['seq'] for row in rows] == [2, 3, 4, 5]
        assert rows[0]['payload']['to'] == 'PROVISIONING'
        assert rows[1]['payload'] == {'count': 1, 'method': 'all'}
        assert rows[2]['payload'] == {'subtask': 'validation', 'turn': 1, 'exit_code': 0}
        assert rows[3]['payload'] == {'passed': True, 'checks': [{'passed': True}]}
        assert all('sentinel-private' not in response.text
                   for response in (list_response, detail, events))

        assert (await client.get(f'/v1/portal/tasks/{other.id}')).status_code == 404
        assert (await client.get(f'/v1/portal/tasks/{other.id}/events')).status_code == 404
        assert (await client.get(f'/v1/tasks/{task.id}/events')).status_code == 401
        assert (await client.get(f'/v1/tasks/{task.id}/artifacts')).status_code == 401
        assert (await client.post(f'/v1/tasks/{task.id}/review', json={'decision': 'approve'})).status_code == 401
        raw = await client.get(f'/v1/tasks/{task.id}/events',
                               headers={'Authorization': 'Bearer token'})
        assert raw.status_code == 200 and 'sentinel-private-command' in raw.text
        assert (await client.get('/v1/portal/tasks', headers={'Host': 'portal.example'})).status_code == 404

        settings.portal_local_task_progress = False
        assert (await client.get('/v1/portal/tasks')).status_code == 404

    settings.portal_local_task_progress = True
    transport = httpx.ASGITransport(app=app, client=('192.0.2.1', 1234))
    async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as remote:
        assert (await remote.get('/v1/portal/tasks')).status_code == 404
