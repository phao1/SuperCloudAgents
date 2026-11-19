from types import SimpleNamespace

import httpx
import pytest

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
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
