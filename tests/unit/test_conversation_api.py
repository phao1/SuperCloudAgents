from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.engine import make_url

from sca.api.app import create_app
from sca.config import Settings
from sca.core.errors import AuthError, ConflictError, NotFoundError
from sca.core.models import Event, Principal, Task, TaskState
from sca.core.task_dag import build_task_dag
from sca.evolve.steering import MESSAGE_EVENT, PROGRESS_EVENT
from sca.evolve.workflow import EvolutionWorkflow

NOW = datetime(2026, 11, 22, tzinfo=UTC)


def auth(role):
    return {'Authorization': f'Bearer {role}'}


@pytest.fixture
async def api(monkeypatch, tmp_path):
    settings = Settings(_env_file=None, guest_view='local', guest_tenant='eval', data_dir=tmp_path)
    monkeypatch.setattr('sca.api.app.get_settings', lambda: settings)
    evolution_id = uuid4()
    header = {'id': evolution_id, 'status': 'running', 'config': {}}
    entries = [{'id': 1, 'type': PROGRESS_EVENT, 'actor': 'workflow', 'created_at': NOW, 'payload': {
        'item_id': 'started', 'kind': 'start', 'text': '开始演进', 'dag_node_id': f'evolution:{evolution_id}',
    }}]
    task_id = uuid4()
    task = Task(id=task_id, tenant_id='eval', kind='coding', spec={}, state=TaskState.RUNNING,
                created_at=NOW, updated_at=NOW)
    events = []

    async def authenticate(token):
        if not token:
            raise AuthError('Missing fixture token')
        tenant = 'other' if token == 'outside' else 'eval'
        return Principal(tenant_id=tenant, subject='operator@example.org',
                         role=token if token in ('member', 'viewer') else 'admin',
                         scopes=['ci:report'] if token == 'restricted' else [])

    async def read(id_, after=0, limit=None, *, messages_only=False, check_legacy=False):
        if id_ != evolution_id:
            raise NotFoundError('演进记录不存在')
        source = [row for row in entries if row['id'] > after and (
            not messages_only or row['type'] == MESSAGE_EVENT or row['payload']['kind'] == 'receipt'
        )]
        return header, False, source[:limit]

    async def add(id_, body, actor):
        await read(id_)
        if header['status'] == 'completed':
            raise ConflictError('演进已结束，无法发送消息')
        row = {'id': len(entries) + 1, 'type': MESSAGE_EVENT, 'actor': actor, 'created_at': NOW, 'payload': {
            'item_id': str(uuid4()), 'evolution_id': str(id_), **body.model_dump(exclude_none=True),
        }}
        entries.append(row)
        return row

    async def task_events(id_, after, limit, milestones, context):
        if id_ != task_id:
            raise NotFoundError('Task not found')
        return task, [event for event in events if event.seq > after and event.type in milestones][:limit], [
            event for event in events if event.type in context
        ]

    handle = SimpleNamespace(signal=AsyncMock())
    temporal = SimpleNamespace(get_workflow_handle=lambda identifier: handle)
    scoped = SimpleNamespace(read_evolution_conversation=read, add_evolution_message=add,
                             task_conversation_events=task_events)
    store = SimpleNamespace(for_tenant=lambda tenant: scoped, tenant_deleted=AsyncMock(return_value=False),
        sessionmaker=SimpleNamespace(kw={'bind': SimpleNamespace(url=make_url('postgresql+asyncpg://localhost/sca'))}))
    app = create_app(store=store, authenticator=SimpleNamespace(authenticate=authenticate),
                     orchestrator=SimpleNamespace(client=temporal))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
        yield client, header, entries, handle, task, events


async def test_evolution_message_auth_scope_terminal_and_receipt(api):
    client, row, entries, handle, _, _ = api
    path = f'/v1/evolutions/{row["id"]}'
    for role, status in ((None, 401), ('viewer', 403), ('outside', 403), ('restricted', 403)):
        response = await client.post(path + '/messages', json={'text': 'Prefer small checks.'},
                                     headers=auth(role) if role else {})
        assert response.status_code == status
    handle.signal.assert_not_awaited()
    for endpoint in ('messages', 'conversation'):
        assert (await client.get(path + '/' + endpoint, headers=auth('viewer'))).status_code == 200
        assert (await client.get(path + '/' + endpoint, headers=auth('outside'))).status_code == 403
        assert (await client.get(f'/v1/evolutions/{uuid4()}/{endpoint}', headers=auth('member'))).status_code == 404

    response = await client.post(path + '/messages', json={'text': 'Ask owner@example.org'}, headers=auth('member'))
    assert response.status_code == 202
    item = response.json()
    assert item['status'] == 'pending' and item['mode'] == 'guidance'
    handle.signal.assert_awaited_once_with(EvolutionWorkflow.user_message, {
        'id': item['id'], 'seq': 2, 'mode': 'guidance', 'text': 'Ask owner@example.org',
    })
    entries.append({'id': 3, 'type': PROGRESS_EVENT, 'actor': 'workflow', 'created_at': NOW + timedelta(seconds=1),
                    'payload': {'item_id': 'receipt:' + item['id'], 'kind': 'receipt', 'status': 'applied',
                                'text': '已应用于第 1 代提议', 'dag_node_id': 'generation:1', 'message_id': item['id']}})
    messages = (await client.get(path + '/messages')).json()
    assert messages == [{key: value for key, value in {**item, 'text': 'Ask [redacted]', 'status': 'applied'}.items()
                         if key != 'actor'}]

    transcript, cursor = [], None
    for expected in ('start', 'message', 'receipt'):
        params = {'limit': 1, **({'cursor': cursor} if cursor is not None else {})}
        page = (await client.get(path + '/conversation', params=params)).json()
        [entry] = page['items']
        assert entry['kind'] == expected and 'actor' not in entry
        transcript.append(entry)
        cursor = page['cursor']
    assert transcript[-1]['dag_node_id'] == 'generation:1'
    assert (await client.get(path + '/conversation', params={'cursor': cursor})).json() == {
        'items': [], 'cursor': cursor, 'has_more': False,
    }
    assert (await client.get(path + '/conversation', params={'cursor': 'wrong'})).status_code == 422

    row['status'] = 'completed'
    ended = await client.post(path + '/messages', json={'mode': 'pause'}, headers=auth('admin'))
    assert ended.status_code == 409 and '演进已结束' in ended.json()['error']['message']
    assert handle.signal.await_count == 1


async def test_task_conversation_filters_raw_events_and_links_to_the_graph(api):
    client, _, _, _, task, events = api
    for seq, (kind, payload) in enumerate([
        ('agent.turn_started', {'turn': 1}), ('agent.message', {'text': 'raw agent output', 'turn': 1}),
        ('agent.command', {'command': 'raw command', 'exit_code': 1, 'turn': 1}),
        ('agent.tool', {'name': 'read', 'turn': 1}), ('agent.tool', {'name': 'read', 'turn': 1}),
        ('agent.turn_completed', {'turn': 1}), ('verification.started', {'turn': 1}),
        ('verification.completed', {'passed': False, 'output_tail': 'raw failed check', 'turn': 1}),
        ('user.message', {'message_id': 'message', 'text': 'Ask owner@example.org', 'actor': 'operator@example.org',
                          'mode': 'queue'}),
        ('user.message.delivered', {'message_id': 'message', 'turn': 2, 'status': 'delivered'}),
        ('agent.turn_started', {'turn': 2}), ('user.message.applied', {'message_id': 'message', 'turn': 2}),
        ('review.requested', {'turn': 2}),
    ], start=1):
        events.append(Event(task_id=task.id, seq=seq, type=kind, payload=payload, created_at=NOW + timedelta(seconds=seq)))
    path = f'/v1/tasks/{task.id}/conversation'
    first = (await client.get(path, params={'limit': 2})).json()
    assert first['has_more'] and first['cursor'] == '6'
    assert [item['kind'] for item in first['items']] == ['agent.turn_started', 'agent.turn_completed']
    assert '工具调用 3 次 · read × 2' in first['items'][1]['text']
    rest = (await client.get(path, params={'cursor': first['cursor']})).json()
    texts = [item['text'] for item in rest['items']]
    assert '验证失败' in texts and '进入人工审核' in texts and '插话已应用于第 2 轮' in texts
    assert 'Ask [redacted]' in texts
    assert all('raw' not in text for text in texts)
    ids = {node.id for node in build_task_dag(task, events, []).nodes}
    assert {item['dag_node_id'] for item in [*first['items'], *rest['items']]} <= ids
    assert (await client.get(path, headers=auth('restricted'))).status_code == 403
    assert (await client.get(f'/v1/tasks/{uuid4()}/conversation')).status_code == 404
