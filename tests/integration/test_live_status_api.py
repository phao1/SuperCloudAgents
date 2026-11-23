import socket
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.archive import archive_task
from sca.config import Settings
from sca.core.models import TaskState
from sca.live_heartbeat import worker_activity
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


@pytest.fixture
def postgres():
    try:
        with socket.create_connection(('localhost', 5432), timeout=.5):
            pass
    except OSError as exc:
        pytest.skip(f'PostgreSQL is unavailable to the sandbox ({exc.strerror})')


@pytest.fixture
async def api(postgres, store, api_store, monkeypatch, tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, guest_view='local', guest_tenant='eval',
                        portal_require_token=True)
    monkeypatch.setattr('sca.api.app.get_settings', lambda: settings)
    objects = LocalObjectStore(tmp_path / 'objects')
    store.objects = objects
    app = create_app(store=api_store, object_store=objects, orchestrator=SimpleNamespace(),
                     authenticator=StaticTokenAuthenticator({'eval-fixture': 'eval', 'other-fixture': 'other'}),
                     local_portal_port=8000)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
        yield client, store, settings


async def test_task_live_reads_events_and_worker_heartbeat_under_rls_and_after_archive(api):
    client, store, settings = api
    owner = store.for_tenant('eval')
    task, _ = await owner.create_task(tenant_id='eval', kind='coding', spec={}, idempotency_key=None)
    await owner.set_state(task.id, TaskState.PROVISIONING)
    await owner.set_state(task.id, TaskState.RUNNING)
    await owner.append_event(task.id, 'agent.turn_started', {'turn': 1, 'backend': 'fake'})
    await owner.append_event(task.id, 'agent.activity', {
        'turn': 1, 'operation': 'command', 'status': 'started', 'call_id': 'command',
        'detail': 'echo fixture@example.test', 'raw': 'fixture raw payload',
    })
    path = f'/v1/tasks/{task.id}/live'
    async with worker_activity(settings.data_dir, 'task', task.id, 'turn'):
        live = await client.get(path)
        assert live.status_code == 200 and live.json()['last_heartbeat_at']
        assert live.json()['agents'][0]['activity']['detail'] == 'echo [redacted]'
        assert 'fixture raw payload' not in live.text
        assert (await client.get(path, headers={'Authorization': 'Bearer other-fixture'})).status_code == 404
        await owner.append_event(task.id, 'agent.command', {
            'turn': 1, 'command': 'echo fixture@example.test', 'exit_code': 0, 'call_id': 'command',
        })
        await owner.append_event(task.id, 'agent.turn_completed', {'turn': 1})
    await owner.set_state(task.id, TaskState.VERIFYING)
    await owner.set_state(task.id, TaskState.AWAITING_REVIEW)
    await owner.set_state(task.id, TaskState.SUCCEEDED)
    before = (await client.get(path)).json()
    await archive_task(owner, store.objects, task.id)
    after = (await client.get(path)).json()
    assert after['finished'] and after['summary'] == before['summary']
    commands = (await client.get(path.replace('/live', '/conversation'))).json()['items']
    command = next(item['activity']['operations'][0] for item in commands if item['kind'] == 'operations')
    assert command['duration_s'] >= 0 and command['exit_code'] == 0


async def test_evolution_live_is_scoped_to_the_evaluation_tenant(api):
    client, store, settings = api
    id_ = uuid4()
    owner = store.for_tenant('eval')
    await owner.create_evolution({'id': id_, 'status': 'running', 'config': {'budget_usd': .25},
                                  'created_by': 'fixture'})
    other = uuid4()
    await store.for_tenant('other').create_evolution({'id': other, 'status': 'running', 'config': {},
                                                     'created_by': 'fixture'})
    async with worker_activity(settings.data_dir, 'evolution', id_, 'proposal'):
        path = f'/v1/evolutions/{id_}/live'
        result = await client.get(path)
        assert result.status_code == 200
        assert result.json()['phase']['operation'] == 'proposal' and result.json()['last_heartbeat_at']
        assert (await client.get(path, headers={'Authorization': 'Bearer other-fixture'})).status_code == 403
        assert (await client.get(f'/v1/evolutions/{other}/live')).status_code == 404
