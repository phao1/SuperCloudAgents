import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.exc import NoResultFound

from sca.agents.fake import FakeBackend
from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.api.live_task import TaskTrace
from sca.config import Settings
from sca.core.errors import NotFoundError
from sca.core.models import AgentContext, Event, ExecExit, Task, TaskState
from sca.live_heartbeat import WorkerBeat, read_beats, worker_activity

START = datetime(2026, 11, 24, tzinfo=UTC)


def at(seconds):
    return START + timedelta(seconds=seconds)


def task(state=TaskState.RUNNING, **spec):
    return Task(id=uuid4(), tenant_id='eval', kind='coding', spec={'agent_backend': 'dsh', **spec},
                state=state, created_at=START, updated_at=at(12))


def events(row, *entries):
    return [Event(task_id=row.id, seq=seq, created_at=at(seconds), type=type_, payload=payload)
            for seq, (seconds, type_, payload) in enumerate(entries, 1)]


@pytest.fixture
async def api(monkeypatch, tmp_path):
    settings = Settings(_env_file=None, guest_view='local', guest_tenant='eval', data_dir=tmp_path,
                        portal_require_token=True)
    monkeypatch.setattr('sca.api.app.get_settings', lambda: settings)
    fixtures = SimpleNamespace(now=at(200), task=task(), events=[], beats=[], evolution=None, case_events={})

    class Clock:
        @staticmethod
        def now(tz):
            return fixtures.now

    monkeypatch.setattr('sca.api.task_dag.datetime', Clock)
    monkeypatch.setattr('sca.api.evolutions.datetime', Clock)
    monkeypatch.setattr('sca.api.task_dag.read_beats', lambda *_: fixtures.beats)
    monkeypatch.setattr('sca.api.live_evolution.read_beats',
                        lambda _data, entity, id_: [beat for owner, beat in fixtures.beats if (entity, id_) == owner])

    def scoped(tenant_id):
        async def task_history(id_, types):
            if tenant_id != 'eval' or id_ != fixtures.task.id:
                raise NotFoundError('Task not found')
            return (fixtures.task, [event for event in fixtures.events if event.type in types],
                    max((event.created_at for event in fixtures.events), default=None))

        async def evolution_history(id_):
            if fixtures.evolution is None or id_ != fixtures.evolution['evolution']['id']:
                raise NoResultFound()
            return fixtures.evolution

        return SimpleNamespace(
            live_task_history=task_history, get_evolution_dag_history=evolution_history,
            live_evolution_event=AsyncMock(return_value=None),
            live_case_events=AsyncMock(side_effect=lambda ids: {
                id_: fixtures.case_events[id_] for id_ in ids if id_ in fixtures.case_events
            }),
        )

    store = SimpleNamespace(
        for_tenant=scoped, tenant_deleted=AsyncMock(return_value=False),
        sessionmaker=SimpleNamespace(kw={'bind': SimpleNamespace(url=make_url('postgresql+asyncpg://localhost/sca'))}),
    )
    app = create_app(store=store, orchestrator=SimpleNamespace(), local_portal_port=8000,
                     authenticator=StaticTokenAuthenticator({'eval-reader': 'eval:viewer', 'other-reader': 'other'}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
        yield client, fixtures


async def test_live_task_shows_lane_dependencies_commands_liveness_and_redacts_guests(api):
    client, fixtures = api
    fixtures.task = row = task(subtasks=[
        {'name': 'a'}, {'name': 'b'}, {'name': 'c', 'depends_on': ['b']}, {'name': 'd'},
        {'name': 'e', 'depends_on': ['a']},
    ])
    fixtures.events = events(row,
        (5, 'task.state_changed', {'to': 'PROVISIONING'}),
        (12, 'task.state_changed', {'to': 'RUNNING'}),
        (13, 'plan.started', {'attempt': 1}),
        (44, 'plan.completed', {'attempt': 1}),
        (45, 'subtasks.scheduled', {'subtasks': [
            {'name': name, 'agent_lane': lane} for name, lane in zip('abcde', [1, 2, 3, 4, 1], strict=True)
        ]}),
        (46, 'subtask.started', {'subtask': 'a'}),
        (47, 'subtask.started', {'subtask': 'b'}),
        (51, 'agent.turn_started', {'subtask': 'a', 'turn': 1}),
        (52, 'agent.turn_started', {'subtask': 'b', 'turn': 1}),
        (54, 'agent.usage', {'subtask': 'a', 'turn': 1, 'input_tokens': 6, 'output_tokens': 4, 'cost': .003}),
        (58, 'agent.activity', {'subtask': 'b', 'turn': 1, 'operation': 'command', 'status': 'started',
                                'detail': 'python -m pytest -q --report=operator@example.test API_KEY=fixture-value'}),
        (160, 'subtask.queued', {'subtask': 'd'}),
    )
    fixtures.beats = [WorkerBeat('turn', ('a', None), at(50), at(192))]
    path = f'/v1/tasks/{row.id}/live'
    response = await client.get(path, headers={'Authorization': 'Bearer eval-reader'})
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    snapshot = response.json()
    assert snapshot['phase']['operation'] == 'subtasks' and snapshot['phase']['started_at'] == '2026-11-24T00:00:45Z'
    assert snapshot['elapsed_s'] == 200 and not snapshot['stalled']
    a, b, c, d = sorted(snapshot['agents'], key=lambda agent: agent['subtask'])
    assert a['label'] == 'Agent 1' and a['backend'] == 'dsh' and a['turn'] == 1
    assert a['activity']['operation'] == 'model' and not a['stalled']
    assert a['tokens'] == 10 and a['cost_usd'] == .003
    assert b['stalled'] and b['activity']['operation'] == 'command'
    assert c['activity']['operation'] == 'dependency' and c['activity']['detail'] == 'b'
    assert d['activity']['operation'] == 'slot' and not d['stalled']
    assert snapshot['last_activity_at'] == snapshot['last_heartbeat_at']
    assert 'operator@example.test' in response.text and 'fixture-value' not in response.text
    guest = await client.get(path)
    assert guest.status_code == 200 and 'operator@example.test' not in guest.text
    assert 'python -m pytest -q' in guest.json()['agents'][1]['activity']['detail']
    assert (await client.get(path, headers={'Authorization': 'Bearer other-reader'})).status_code == 404


async def test_live_task_stall_threshold_and_finished_clock(api):
    client, fixtures = api
    row = fixtures.task
    path = f'/v1/tasks/{row.id}/live'
    fixtures.events = events(row,
        (5, 'task.state_changed', {'to': 'PROVISIONING'}),
        (12, 'task.state_changed', {'to': 'RUNNING'}),
        (20, 'agent.turn_started', {'turn': 1}),
    )
    fixtures.now = at(140)
    assert not (await client.get(path)).json()['stalled']
    fixtures.now = at(141)
    assert (await client.get(path)).json()['stalled']
    fixtures.beats = [WorkerBeat('turn', ('', None), at(19), at(138))]
    assert not (await client.get(path)).json()['stalled']
    fixtures.task.state = TaskState.SUCCEEDED
    fixtures.task.updated_at = at(150)
    fixtures.events += [
        Event(task_id=row.id, seq=4, type='agent.turn_completed', created_at=at(60),
              payload={'turn': 1, 'cost_usd': .004}),
        Event(task_id=row.id, seq=5, type='task.state_changed', created_at=at(61), payload={'to': 'VERIFYING'}),
        Event(task_id=row.id, seq=6, type='task.state_changed', created_at=at(70), payload={'to': 'SUCCEEDED'}),
    ]
    finished = (await client.get(path)).json()
    assert finished['finished'] and not finished['stalled'] and finished['elapsed_s'] == 70
    assert finished['summary']['agents'][0]['duration_s'] == 40
    assert finished['cost_usd'] == .004
    assert sum(phase['duration_s'] for phase in finished['summary']['phases']) == 70


@pytest.mark.parametrize(('state', 'operation', 'expected', 'running'), [
    (TaskState.QUEUED, None, 'queue', False),
    (TaskState.PROVISIONING, 'sandbox', 'setup', True),
    (TaskState.RUNNING, 'plan', 'plan', True),
    (TaskState.RUNNING, 'merge', 'merge', True),
    (TaskState.VERIFYING, 'verification', 'verification', True),
    (TaskState.AWAITING_REVIEW, 'output', 'output', True),
    (TaskState.AWAITING_REVIEW, None, 'review', False),
    (TaskState.PAUSED, None, 'paused', False),
])
def test_task_phase_and_intentional_waits(state, operation, expected, running):
    row = task(state)
    history = events(row, (12, 'task.state_changed', {'to': state}))
    heartbeats = [WorkerBeat(operation, ('', None), at(15), at(194))] if operation else []
    result = TaskTrace(row, history, 'codex').snapshot(at(200), 120, at(12), heartbeats)
    assert result.phase.operation == expected and result.running == running
    assert not result.stalled


async def test_live_evolution_progress_and_liveness_follow_the_current_case(api):
    client, fixtures = api
    id_, run, candidate, case = (uuid4() for _ in range(4))
    fixtures.evolution = {
        'evolution': {'id': id_, 'status': 'running', 'created_at': START, 'updated_at': at(10),
                      'finished_at': None, 'config': {'budget_usd': .25}, 'spent_usd': 0},
        'generations': [{'generation': 2, 'created_at': at(20)}],
        'candidates': [{'id': candidate, 'generation': 2, 'created_at': at(40), 'decision': 'pending',
                        'overlay': {}, 'hypothesis': 'Check bounds'}],
        'runs': [{'id': run, 'status': 'running', 'created_at': at(46), 'finished_at': None, 'variant': {
            'repeat': 2, 'split': 'dev', 'case_metadata': {str(i): {} for i in range(5)},
            'evolution_step': {'candidate_id': str(candidate), 'role': 'candidate', 'generation': 2},
        }}],
        'tasks': [{'id': case, 'state': 'RUNNING', 'created_at': at(50), 'updated_at': at(50),
                   'eval_run_id': str(run), 'idempotency_key': f'eval:{run}:operator@example.test:0',
                   'cost_micro_usd': 2000}],
        'results': [{'run_id': run, 'task_id': uuid4(), 'case_id': f'completed-{i}', 'repeat': 0,
                     'cost_usd': 0, 'acceptance_passed': i < 2, 'trajectory_passed': None} for i in range(3)],
        'champions': [], 'proposer_spent_usd': .003,
    }
    fixtures.case_events = {case: at(51)}
    path = f'/v1/evolutions/{id_}/live'
    snapshot = (await client.get(path)).json()
    assert snapshot['stalled'] and snapshot['evaluation']['cases'][0]['stalled']
    assert snapshot['generation'] == 2 and snapshot['candidate'] == 1
    eval_progress = snapshot['evaluation']
    assert (eval_progress['completed'], eval_progress['total'], eval_progress['passed']) == (3, 10, 2)
    assert eval_progress['cases'][0]['started_at'] == '2026-11-24T00:00:50Z'
    assert 'operator@example.test' not in eval_progress['cases'][0]['label']
    assert snapshot['cost_usd'] == .005 and snapshot['budget_usd'] == .25
    fixtures.beats = [(('task', case), WorkerBeat('turn', ('', None), at(51), at(193)))]
    assert not (await client.get(path)).json()['stalled']
    assert (await client.get(path, headers={'Authorization': 'Bearer other-reader'})).status_code == 403


async def test_worker_heartbeat_refreshes_and_removes_independent_lane_markers(monkeypatch, tmp_path):
    monkeypatch.setattr('sca.live_heartbeat.HEARTBEAT_SECONDS', .01)
    id_ = uuid4()
    async with worker_activity(tmp_path, 'task', id_, 'turn', ('a', None)):
        [original] = read_beats(tmp_path, 'task', id_)
        async with worker_activity(tmp_path, 'task', id_, 'verification', ('b', None)):
            async with asyncio.timeout(1):
                while True:
                    active = {beat.scope[0]: beat for beat in read_beats(tmp_path, 'task', id_)}
                    if active['a'].at > original.at:
                        break
                    await asyncio.sleep(.01)
            assert active['a'].started_at == original.started_at
            assert active['b'].operation == 'verification'
        assert [beat.scope for beat in read_beats(tmp_path, 'task', id_)] == [('a', None)]
    assert not read_beats(tmp_path, 'task', id_)


async def test_fake_reports_command_before_running_the_script(tmp_path):
    launched = False

    async def run(*args, **kwargs):
        nonlocal launched
        launched = True
        yield ExecExit(exit_code=3)

    context = AgentContext(task_id='fixture', agent_home_host=str(tmp_path))
    fence = chr(96) * 3
    stream = FakeBackend().run_turn(SimpleNamespace(exec=run), 'sandbox', context,
                                    f'{fence}sca-fake\nexit 3\n{fence}', session_id='session', timeout_s=5)
    started = await anext(stream)
    assert started.type == 'activity' and started.data['operation'] == 'command' and not launched
    complete, *_ = [event async for event in stream]
    assert complete.data['exit_code'] == 3 and complete.data['duration_s'] >= 0
