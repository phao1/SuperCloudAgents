import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import yaml
from sqlalchemy.engine import make_url

from sca.api import portal
from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.gateway.configuration import deepseek_configured, local_default_model, model_configured
from sca.gateway.model_providers import gateway_config, read_registry
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
PROVIDERS = '/v1/portal/providers'
ADMIN = {'Authorization': 'Bearer local-admin'}
FIXTURE_KEY = 'fixture-only-$provider-key'
MODEL = {
    'name': '本机模型', 'type': 'openai-compatible', 'base_url': 'http://localhost:8081/v1',
    'models': [{'name': 'local', 'upstream_model': 'org/test-model', 'context_window': 8192},
               {'name': 'other-local', 'upstream_model': 'org/other-model'}],
}


@pytest.fixture
async def provider_api(tmp_path, monkeypatch):
    config = tmp_path / 'fixture.env'
    config.touch()
    monkeypatch.setenv('SCA_PORTAL_LOCAL_CONFIG', str(config))
    monkeypatch.setenv('SCA_RUNTIME_MODE', 'source')
    settings = Settings(_env_file=None, model_gateway_url='http://gateway', data_dir=tmp_path,
                        model_gateway_master_key='fixture-gateway', portal_require_token=True,
                        eval_api_token='local-admin')
    for module in ('sca.api.app', 'sca.api.portal', 'sca.config'):
        monkeypatch.setattr(f'{module}.get_settings', lambda: settings)

    applied = []

    async def restart(command):
        assert command[-1] == 'restart-gateway' and not any(FIXTURE_KEY in arg for arg in command)
        generated = gateway_config(ROOT, tmp_path, read_registry(tmp_path),
                                   deepseek=deepseek_configured(config), docker=False)
        applied[:] = yaml.safe_load(generated.read_text())['model_list']
        return True

    fake_restart = AsyncMock(side_effect=restart)
    monkeypatch.setattr(portal, 'restart_gateway', fake_restart)
    # The registry is installation-local. Only the guest tenant check needs the store.
    store = SimpleNamespace(
        sessionmaker=SimpleNamespace(kw={'bind': SimpleNamespace(
            url=make_url('postgresql+asyncpg://localhost/sca'))}), tenant_deleted=AsyncMock(return_value=False),
    )
    app = create_app(store=store, orchestrator=SimpleNamespace(), object_store=LocalObjectStore(tmp_path),
                     local_portal_port=8000,
                     authenticator=StaticTokenAuthenticator({'local-admin': 'eval:admin',
                         'member': 'eval:member', 'viewer': 'eval:viewer', 'other-admin': 'other:admin'}))
    response_body = {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]}
    upstream_status = [200]

    def gateway(request):
        if request.url.path == '/health/liveliness':
            return httpx.Response(200)
        assert request.headers['Authorization'] == 'Bearer fixture-gateway'
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': row['model_name']} for row in applied]})
        assert request.url.path == '/v1/chat/completions'
        assert b'"model":"local"' in request.content and b'"max_tokens":16' in request.content
        assert FIXTURE_KEY.encode() not in request.content
        return httpx.Response(upstream_status[0], json=response_body if upstream_status[0] == 200 else {
            'error': f'Incorrect API key provided: {FIXTURE_KEY}',
        })

    async_client = httpx.AsyncClient
    monkeypatch.setattr(portal.httpx, 'AsyncClient', lambda **opts: async_client(
        transport=httpx.MockTransport(gateway), **opts))
    async with (
        async_client(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 4321)),
                     base_url='http://127.0.0.1:8000') as local,
        async_client(transport=httpx.ASGITransport(app=app, client=('192.0.2.2', 4321)),
                     base_url='http://127.0.0.1:8000') as remote,
    ):
        yield local, remote, fake_restart, upstream_status, tmp_path


def restart_path(response):
    assert response.status_code == 202
    restart = response.json()['restart']
    assert restart['status'] == 'restarting'
    return f"{PROVIDERS}/restarts/{restart['id']}"


async def check_restart(local, endpoint, expected='succeeded'):
    async with asyncio.timeout(3):
        while True:
            response = await local.get(endpoint, headers=ADMIN)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'
            result = response.json()
            if result['status'] != 'restarting':
                assert result == {'id': endpoint.rsplit('/', 1)[1], 'status': expected}
                return
            await asyncio.sleep(0.01)


async def test_provider_crud_catalog_and_connection(provider_api):
    local, _, restart, upstream, data_dir = provider_api
    assert not model_configured()
    assert (await local.get(PROVIDERS, headers=ADMIN)).json() == {
        'providers': [], 'deepseek_models': [], 'deepseek_enabled': True, 'deepseek_has_key': False,
        'default_model': None,
    }
    r = await local.post(PROVIDERS, headers=ADMIN, json={**MODEL, 'api_key': FIXTURE_KEY})
    await check_restart(local, restart_path(r))
    assert model_configured() and FIXTURE_KEY not in r.text and 'api_key' not in r.json()['provider']
    provider = r.json()['provider']
    assert provider['has_key'] is True
    endpoint = PROVIDERS + '/' + provider['id']
    listed = await local.get(PROVIDERS, headers=ADMIN)
    assert listed.json()['providers'] == [provider] and FIXTURE_KEY not in listed.text
    assert (await local.get(endpoint, headers=ADMIN)).json() == provider
    assert (await local.post(PROVIDERS, headers=ADMIN, json=MODEL)).status_code == 409

    catalog = (await local.get('/v1/models', headers={'Authorization': 'Bearer member'})).json()
    assert catalog == {
        'data': [{'id': 'local', 'context_window': 8192, 'unpriced': True},
                 {'id': 'other-local', 'context_window': None, 'unpriced': True}],
        'default_model': 'local', 'evolve_model': 'local',
    }
    config = (await local.get('/v1/portal/config', headers=ADMIN)).json()
    assert config['models'] == ['local', 'other-local']
    assert config['evolve_model'] == config['default_model'] == 'local'
    assert config['unpriced_models'] == config['models']
    assert MODEL['base_url'] not in str(config)

    r = await local.put(endpoint, headers=ADMIN, json={**MODEL, 'name': 'Updated'})
    await check_restart(local, restart_path(r))
    assert r.json()['provider']['has_key'] is True and FIXTURE_KEY not in r.text
    assert list(read_registry(data_dir).environment().values()) == [FIXTURE_KEY]
    disguised = await local.put(endpoint, headers=ADMIN, json={**MODEL, 'base_url': 'https://other.example/v1'})
    assert disguised.status_code == 409 and FIXTURE_KEY not in disguised.text
    invalid = await local.put(endpoint, headers=ADMIN, json={**MODEL, 'api_key': FIXTURE_KEY + '\n'})
    assert invalid.status_code == 422 and FIXTURE_KEY not in invalid.text
    r = await local.put(endpoint, headers=ADMIN, json={**MODEL, 'api_key': None})
    await check_restart(local, restart_path(r))
    assert r.json()['provider']['has_key'] is False
    assert not read_registry(data_dir).environment()

    r = await local.post(PROVIDERS + '/test', headers=ADMIN, json={'model': 'local'})
    assert r.status_code == 200 and r.json()['ok'] and r.json()['latency_ms'] >= 0
    upstream[0] = 401
    r = await local.post(PROVIDERS + '/test', headers=ADMIN, json={'model': 'local'})
    assert r.status_code == 200 and not r.json()['ok'] and '认证失败' in r.json()['message']
    assert FIXTURE_KEY not in r.text
    deleted = await local.delete(endpoint, headers=ADMIN)
    await check_restart(local, restart_path(deleted))
    assert 'provider' not in deleted.json()
    assert not model_configured()
    assert (await local.delete(endpoint, headers=ADMIN)).status_code == 404
    assert (await local.post(PROVIDERS + '/test', headers=ADMIN, json={'model': 'local'})).status_code == 404
    assert restart.await_count == 4


async def test_provider_controls_apply_to_gateway_catalog_and_task_default(provider_api):
    local, remote, restart, _, data_dir = provider_api
    key_path = '/v1/portal/provider-key'
    deepseek_path = PROVIDERS + '/deepseek/enabled'
    default_path = PROVIDERS + '/default'
    deepseek_key = 'fixture-deepseek-key'

    for path, method, body in [(deepseek_path, 'PUT', {'enabled': False}),
                               (default_path, 'PUT', {'model': None}), (key_path, 'DELETE', None)]:
        assert (await local.request(method, path, json=body)).status_code == 401
        assert (await remote.request(method, path, headers=ADMIN, json=body)).status_code == 403
        assert (await local.request(method, path, headers={'Authorization': 'Bearer member'},
                                    json=body)).status_code == 403
    assert (await local.put(deepseek_path, headers=ADMIN, json={'enabled': True})).status_code == 409
    saved = await local.post(key_path, headers=ADMIN, json={'key': deepseek_key})
    assert saved.json() == {'saved': True, 'gateway_restarted': True}
    assert deepseek_key not in saved.text

    created = await local.post(PROVIDERS, headers=ADMIN, json={**MODEL, 'api_key': FIXTURE_KEY})
    await check_restart(local, restart_path(created))
    provider_path = PROVIDERS + '/' + created.json()['provider']['id']
    enabled_path = provider_path + '/enabled'
    assert (await local.put(enabled_path, headers={'Authorization': 'Bearer member'},
                            json={'enabled': False})).status_code == 403
    assert (await local.put(default_path, headers=ADMIN, json={'model': 'absent'})).status_code == 422
    response = await local.put(default_path, headers=ADMIN, json={'model': 'other-local'})
    assert response.json() == {'model': 'other-local'}
    assert local_default_model(data_dir, 'deepseek-flash') == 'other-local'
    assert restart.await_count == 2

    async def catalog():
        return (await local.get('/v1/models', headers=ADMIN)).json()

    assert (await catalog())['default_model'] == 'other-local'
    disabled = await local.put(enabled_path, headers=ADMIN, json={'enabled': False})
    assert disabled.json()['provider']['has_key'] is True
    await check_restart(local, restart_path(disabled))
    assert local_default_model(data_dir, 'deepseek-flash') == 'deepseek-flash'
    assert (await catalog())['default_model'] == 'deepseek-flash'
    assert [row['id'] for row in (await catalog())['data']] == ['deepseek-flash', 'deepseek-v4-pro']
    denied = await local.post(PROVIDERS + '/test', headers=ADMIN, json={'model': 'other-local'})
    assert denied.status_code == 404
    assert (await local.post(PROVIDERS, headers=ADMIN, json=MODEL)).status_code == 409

    edited = await local.put(provider_path, headers=ADMIN, json={**MODEL, 'name': 'Updated'})
    assert edited.json()['provider']['enabled'] is False
    await check_restart(local, restart_path(edited))
    enabled = await local.put(enabled_path, headers=ADMIN, json={'enabled': True})
    await check_restart(local, restart_path(enabled))
    assert (await catalog())['default_model'] == 'other-local'
    assert FIXTURE_KEY not in enabled.text

    disabled_deepseek = await local.put(deepseek_path, headers=ADMIN, json={'enabled': False})
    await check_restart(local, restart_path(disabled_deepseek))
    assert [row['id'] for row in (await catalog())['data']] == ['local', 'other-local']
    listed = await local.get(PROVIDERS, headers=ADMIN)
    assert listed.json()['deepseek_has_key'] is True and listed.json()['deepseek_models'] == []
    assert deepseek_key not in listed.text and FIXTURE_KEY not in listed.text
    enabled_deepseek = await local.put(deepseek_path, headers=ADMIN, json={'enabled': True})
    await check_restart(local, restart_path(enabled_deepseek))
    assert 'deepseek-flash' in [row['id'] for row in (await catalog())['data']]
    assert (await local.delete(key_path, headers=ADMIN)).json()['gateway_restarted'] is True
    assert (await local.get(PROVIDERS, headers=ADMIN)).json()['deepseek_has_key'] is False
    assert [row['id'] for row in (await catalog())['data']] == ['local', 'other-local']
    assert (await local.put(default_path, headers=ADMIN, json={'model': None})).json() == {'model': None}
    assert (await catalog())['default_model'] == 'local'


async def test_registry_requires_local_eval_admin_and_hides_from_guests(provider_api, monkeypatch):
    local, remote, restart, _, _ = provider_api
    created = await local.post(PROVIDERS, headers=ADMIN, json=MODEL)
    restart_endpoint = restart_path(created)
    await check_restart(local, restart_endpoint)
    endpoint = PROVIDERS + '/' + created.json()['provider']['id']
    guest = await local.get('/v1/portal/config')
    assert guest.json() == {'guest_view': True, 'guest_tenant': 'eval', 'model_configured': True}
    assert (await local.get('/v1/models')).status_code == 401
    assert (await local.get(PROVIDERS)).status_code == 401
    assert (await local.get(endpoint)).status_code == 401
    denied = await remote.get(PROVIDERS, headers=ADMIN)
    assert denied.status_code == 403 and '本机访问' in denied.json()['error']['message']
    assert (await remote.post(PROVIDERS, headers=ADMIN, json=MODEL)).status_code == 403
    assert (await remote.get(restart_endpoint, headers=ADMIN)).status_code == 403
    assert (await local.get(restart_endpoint)).status_code == 401
    for token in ('viewer', 'member', 'other-admin'):
        headers = {'Authorization': 'Bearer ' + token}
        assert (await local.get(PROVIDERS, headers=headers)).status_code == 403
        assert (await local.post(PROVIDERS, headers=headers, json=MODEL)).status_code == 403
        assert (await local.post(PROVIDERS + '/test', headers=headers, json={'model': 'local'})).status_code == 403
        assert (await local.get(restart_endpoint, headers=headers)).status_code == 403
    denied = await local.get(PROVIDERS, headers={'Authorization': 'Bearer other-admin'})
    assert 'eval 租户' in denied.json()['error']['message']
    monkeypatch.delenv('SCA_PORTAL_LOCAL_CONFIG')
    denied = await local.get(PROVIDERS, headers=ADMIN)
    assert denied.status_code == 403 and '缺少本机模型接入配置' in denied.json()['error']['message']
    assert restart.await_count == 1


async def test_local_no_token_session_can_manage_model_providers(provider_api, monkeypatch):
    local, remote, restart, _, _ = provider_api
    switch = await local.put('/v1/portal/access', headers=ADMIN, json={'require_token': False})
    assert switch.json() == {'require_token': False, 'local_no_token_active': True}
    config = (await local.get('/v1/portal/config')).json()
    assert config['local_identity']['tenant_id'] == 'eval' and config['can_configure_model'] is True
    origin = {'Origin': 'http://127.0.0.1:8000'}
    created = await local.post(PROVIDERS, headers=origin, json=MODEL)
    endpoint = restart_path(created)
    await check_restart(local, endpoint)
    assert (await local.get(PROVIDERS)).json()['providers'][0]['name'] == MODEL['name']
    assert (await local.post(PROVIDERS, json=MODEL)).status_code == 401
    assert (await remote.get(PROVIDERS)).status_code == 401
    monkeypatch.delenv('SCA_PORTAL_LOCAL_CONFIG')
    assert (await local.get(PROVIDERS)).status_code == 403
    assert restart.await_count == 1


@pytest.mark.parametrize('verb', ['POST', 'PUT', 'DELETE'])
async def test_provider_change_returns_before_gateway_restart_finishes(provider_api, verb):
    local, _, restart, _, data_dir = provider_api
    endpoint = PROVIDERS
    if verb != 'POST':
        created = await local.post(PROVIDERS, headers=ADMIN, json=MODEL)
        await check_restart(local, restart_path(created))
        endpoint += '/' + created.json()['provider']['id']

    started, finish = asyncio.Event(), asyncio.Event()

    async def slow_restart(command):
        started.set()
        await finish.wait()
        return False

    restart.side_effect = slow_restart
    body = None if verb == 'DELETE' else {**MODEL, 'name': 'Saved'}
    try:
        async with asyncio.timeout(3):
            response = await local.request(verb, endpoint, headers=ADMIN, json=body)
            status_url = restart_path(response)
            await started.wait()
            status = await local.get(status_url, headers=ADMIN)
            assert status.json()['status'] == 'restarting'
            names = [row.name for row in read_registry(data_dir).providers]
            assert names == ([] if verb == 'DELETE' else ['Saved'])
            busy = await local.post(PROVIDERS, headers=ADMIN, json=MODEL)
            assert busy.status_code == 409 and '正在重启' in busy.json()['error']['message']
            builtin = await local.post('/v1/portal/provider-key', headers=ADMIN, json={'key': 'fixture-builtin'})
            assert builtin.status_code == 409
            test = await local.post(PROVIDERS + '/test', headers=ADMIN, json={'model': 'local'})
            assert test.status_code == 409
    finally:
        finish.set()
    await check_restart(local, status_url, 'failed')
    assert (await local.get(f'{PROVIDERS}/restarts/{uuid4()}', headers=ADMIN)).status_code == 404
