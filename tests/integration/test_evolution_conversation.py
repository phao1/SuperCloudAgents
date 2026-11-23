import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import insert
from sqlalchemy.exc import DBAPIError

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.archive import archive_task
from sca.config import Settings
from sca.core.errors import NotFoundError
from sca.core.models import Principal, TaskMessageInput, TaskState
from sca.db.schema import evolution_events
from sca.evolve.steering import MESSAGE_EVENT
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


@pytest.fixture
def postgres():
    try:
        with socket.create_connection(('localhost', 5432), timeout=.5):
            pass
    except OSError as exc:
        pytest.skip(f'PostgreSQL is unavailable to the sandbox ({exc.strerror})')


async def test_messages_persist_with_rls_and_budget_receipts(postgres, store, api_store, monkeypatch, tmp_path):
    evolution_id, other_id = uuid4(), uuid4()
    worker = store.for_tenant('eval')
    await worker.create_evolution({'id': evolution_id, 'status': 'running', 'config': {'budget_usd': 1},
                                   'created_by': 'operator'})
    await store.for_tenant('other').create_evolution({'id': other_id, 'status': 'running', 'config': {},
                                                     'created_by': 'operator'})
    await store.set_tenant_budget('eval', daily_cost_budget_usd=.30)
    with pytest.raises(DBAPIError) as denied:
        async with api_store.for_tenant('eval')._transaction() as session:
            await session.execute(insert(evolution_events).values(
                tenant_id='eval', type=MESSAGE_EVENT, actor='member',
                payload={'evolution_id': str(other_id), 'item_id': str(uuid4()), 'mode': 'guidance', 'text': 'Hint'},
            ))
    assert denied.value.orig.sqlstate == '42501'

    settings = Settings(_env_file=None, guest_view='local', guest_tenant='eval', data_dir=tmp_path)
    monkeypatch.setattr('sca.api.app.get_settings', lambda: settings)
    handle = SimpleNamespace(signal=AsyncMock())
    client = SimpleNamespace(get_workflow_handle=lambda workflow_id: handle)
    app = create_app(store=api_store, authenticator=StaticTokenAuthenticator({'member': 'eval:member'}),
                     orchestrator=SimpleNamespace(client=client))
    path = f'/v1/evolutions/{evolution_id}'
    headers = {'Authorization': 'Bearer member'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as http:
        for requested, total in ((1, None), (.1, .2)):
            response = await http.post(path + '/messages', headers=headers, json={
                'mode': 'budget', 'remaining_budget_usd': requested,
            })
            assert response.status_code == 202
            sent = handle.signal.call_args.args[1]
            for _ in range(2):
                assert await worker.apply_evolution_budget(evolution_id, sent, .1, f'evolution:{evolution_id}') == total
        messages = (await http.get(path + '/messages')).json()
        assert [message['status'] for message in messages] == ['rejected', 'applied']
        assert all('actor' not in message for message in messages)
        assert (await worker.get_evolution(str(evolution_id)))['config']['budget_usd'] == .2
        with pytest.raises(NotFoundError, match='演进记录不存在'):
            await api_store.for_tenant('other').read_evolution_conversation(evolution_id)


@pytest.mark.parametrize('archived', [False, True])
async def test_task_conversation_cursor_has_the_same_node_ids_after_archive(postgres, store, tmp_path, archived):
    from sca.api.conversation import task_page

    tenant = 'eval'
    owner = store.for_tenant(tenant)
    task, _ = await owner.create_task(tenant_id=tenant, kind='coding', spec={}, idempotency_key=None)
    await owner.set_state(task.id, TaskState.PROVISIONING)
    await owner.set_state(task.id, TaskState.RUNNING)
    await owner.append_event(task.id, 'agent.turn_started', {'turn': 1})
    await owner.append_event(task.id, 'agent.tool', {'name': 'read', 'turn': 1, 'text': 'fixture log'})
    await owner.append_event(task.id, 'agent.turn_completed', {'turn': 1})
    await owner.set_state(task.id, TaskState.FAILED, 'Fixture failed')
    objects = LocalObjectStore(tmp_path / 'objects')
    owner.objects = objects
    if archived:
        await archive_task(owner, objects, task.id)
    user = Principal(tenant_id=tenant, subject='reader', role='viewer')
    first = await task_page(owner, task.id, user, 0, 5)
    second = await task_page(owner, task.id, user, int(first.cursor), 3)
    assert first.has_more and not second.has_more
    items = [*first.items, *second.items]
    assert [item.kind for item in items] == [
        'task.created', 'task.state_changed', 'task.state_changed', 'agent.turn_started', 'operations',
        'agent.turn_completed', 'task.state_changed',
    ]
    assert items[3].dag_node_id == items[4].dag_node_id == items[5].dag_node_id
    assert items[4].activity.operations[0].name == 'read'
    assert 'fixture log' not in first.model_dump_json()


async def test_turn_start_receipts_are_atomic_scoped_and_retry_safe(postgres, store, api_store):
    owner = store.for_tenant('team')
    task, _ = await owner.create_task(tenant_id='team', kind='coding', spec={}, idempotency_key=None)
    team = api_store.for_tenant('team')
    other = api_store.for_tenant('other')
    sent = await team.add_task_message(task.id, TaskMessageInput(text='修复输入', mode='interrupt'), 'member')
    late = await team.add_task_message(task.id, TaskMessageInput(text='再检查输出'), 'member')
    assert [message.status for message in await team.list_task_messages(task.id)] == ['pending', 'pending']
    assert [message.id for message in await owner.pending_task_messages(str(task.id))] == [sent.id, late.id]
    with pytest.raises(NotFoundError):
        await other.list_task_messages(task.id)
    args = (str(task.id), {'turn': 2}, 'turn2.attempt1.started', [str(sent.id)], [str(sent.id)])
    await owner.start_guided_turn(*args)
    await owner.start_guided_turn(*args)
    events = await owner.list_events(task.id)
    assert [event.type for event in events[-2:]] == ['agent.turn_started', 'user.message.delivered']
    assert events[-1].payload['message_id'] == str(sent.id) and events[-1].payload['interrupted'] is True
    assert [message.status for message in await team.list_task_messages(task.id)] == ['delivered', 'pending']
    await owner.apply_task_messages(str(task.id), [str(sent.id)])
    assert [message.status for message in await team.list_task_messages(task.id)] == ['applied', 'pending']


async def test_approvals_inbox_reads_only_pending_rows_for_the_token_tenant(postgres, store, api_store, monkeypatch,
                                                                    tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    monkeypatch.setattr('sca.api.app.get_settings', lambda: settings)

    async def fixtures(tenant):
        scoped = store.for_tenant(tenant)
        project = await scoped.create_project({'name': f'Project for {tenant}'}, 'fixture')
        plan, _ = await scoped.create_task(
            tenant_id=tenant, kind='coding', idempotency_key=None, project_id=project.id,
            spec={'instruction': f'## **Plan** for {tenant}\n\n```sca-fake\necho fixture\n```'},
        )
        await scoped.set_state(plan.id, TaskState.PROVISIONING)
        await scoped.set_state(plan.id, TaskState.RUNNING)
        await scoped.set_state(plan.id, TaskState.VERIFYING)
        await scoped.set_session(plan.id, 'fake', 'fixture-session')
        await scoped.append_event(plan.id, 'review.requested', {'plan': {'subtasks': []}})
        started = await scoped.set_state(plan.id, TaskState.AWAITING_REVIEW)
        await scoped.update_result(plan.id, {'fixture': True})
        await scoped.create_task(tenant_id=tenant, kind='coding', spec={}, idempotency_key=None)
        run, candidate, harvest = uuid4(), uuid4(), uuid4()
        await scoped.create_evolution({'id': run, 'status': 'awaiting_approval', 'config': {}, 'created_by': 'fixture'})
        proposal = {'id': candidate, 'evolution_id': run, 'generation': 1, 'overlay': {}, 'overlay_sha256': 'fixture',
                    'hypothesis': f'Candidate for {tenant}', 'diagnosis': {}, 'static_check': {}, 'decision': 'pending',
                    'reason': 'awaiting approval'}
        await scoped.save_evolution_candidate(proposal)
        await scoped.save_evolution_candidate({**proposal, 'id': uuid4(), 'reason': None})
        await scoped.create_harvest_candidate({
            'id': harvest, 'task_id': plan.id, 'suite': 'fixture', 'split': 'dev', 'case_id': 'regression',
            'base_sha': 'fixture', 'notes': '', 'created_by': 'fixture',
        })
        await scoped.create_harvest_candidate({
            'id': uuid4(), 'task_id': plan.id, 'suite': 'fixture', 'split': 'dev', 'case_id': 'already-published',
            'base_sha': 'fixture', 'notes': '', 'created_by': 'fixture', 'status': 'approved',
        })
        return started, candidate, harvest

    plan, candidate, harvest = await fixtures('eval')
    other_plan, other_candidate, other_harvest = await fixtures('other')
    app = create_app(store=api_store, authenticator=StaticTokenAuthenticator({'eval': 'eval', 'other': 'other'}),
                     orchestrator=SimpleNamespace())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for tenant, task, expected in [
            ('eval', plan, {'task_plan': plan.id, 'evolution_candidate': candidate, 'harvest': harvest}),
            ('other', other_plan, {'task_plan': other_plan.id, 'harvest': other_harvest}),
        ]:
            response = await client.get('/v1/approvals?tenant_id=other',
                                        headers={'Authorization': f'Bearer {tenant}'})
            assert response.status_code == 200
            assert response.json()['count'] == len(expected)
            items = {row['kind']: row for row in response.json()['items']}
            assert {kind: item['id'] for kind, item in items.items()} == {
                kind: str(id_) for kind, id_ in expected.items()
            }
            item = items['task_plan']
            assert item['title'] == f'Plan for {tenant}'
            assert item['project_name'] == f'Project for {tenant}'
            assert item['backend'] == 'fake' and item['task_kind'] == 'coding'
            assert item['created_at'] == task.model_dump(mode='json')['updated_at']
            assert str(other_candidate) not in response.text
