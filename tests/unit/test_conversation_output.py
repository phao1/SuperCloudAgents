import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_opencode_fixture import TRANSCRIPT as OPENCODE_TRACE
from test_pi_fixture import TRANSCRIPT as PI_TRACE

from sca.agents.codex import parse_codex_line
from sca.agents.dsh import _RunEvents
from sca.agents.opencode import parse_opencode_line
from sca.agents.pi import parse_pi_line
from sca.api.authz import GuestPrincipal
from sca.api.conversation import task_page
from sca.core.models import AgentEvent, Event, Principal, Task, TaskState

NOW = datetime(2026, 11, 22, tzinfo=UTC)
OWNER = Principal(tenant_id='team', subject='reviewer')


def history(backend, payloads):
    task = Task(id=uuid4(), tenant_id='team', kind='coding', state=TaskState.RUNNING, spec={'agent_backend': 'fake'},
                created_at=NOW, updated_at=NOW)
    events = [Event(task_id=task.id, seq=1, type='agent.turn_started',
                    payload={'backend': backend, 'turn': 1, 'subtask': 'compile'}, created_at=NOW)]
    for number, (kind, data) in enumerate(payloads, start=2):
        events.append(Event(task_id=task.id, seq=number, type=kind,
                            payload={'turn': 1, 'subtask': 'compile', **data}, created_at=NOW))
    return task, events


async def project(task, events, *, user=OWNER, after=0, limit=200):
    async def read(id_, cursor, size, milestones, context):
        assert id_ == task.id
        selected = [event for event in events if event.seq > cursor and event.type in milestones][:size]
        compact = [event for event in events if event.type in context]
        return task, selected, compact

    return await task_page(SimpleNamespace(task_conversation_events=read), task.id, user, after, limit)


async def test_conversation_reports_plan_cap_and_labeled_subtask_progress():
    task, events = history('dsh', [
        ('plan.completed', {'subtasks': [{'name': name} for name in ('compile', 'test')],
                            'split': True, 'max_parallel_agents': 2}),
        ('subtask.started', {'backend': 'dsh'}),
        ('subtask.completed', {'backend': 'dsh', 'passed': True}),
    ])
    page = await project(task, events)
    plan, start, finish = [item for item in page.items if item.kind.startswith(('plan.', 'subtask.'))]
    assert plan.text == '规划完成 · 2 个子任务，最多 2 个并发'
    assert (start.text, finish.text) == ('compile · 开始', 'compile · 完成')
    assert start.backend == finish.backend == 'dsh'
    assert start.dag_node_id == finish.dag_node_id == f'task:{task.id}/subtask:compile'


async def test_conversation_projects_lane_labels_for_progress_and_replies():
    lane = {'backend': 'dsh', 'agent_lane': 2, 'agent_label': 'Agent 2 · DeepSeek Harness'}
    task, events = history('dsh', [
        ('subtask.started', lane),
        ('agent.message', {'text': 'Changes are ready'}),
        ('subtask.completed', {**lane, 'passed': True}),
    ])
    page = await project(task, events)
    items = [item for item in page.items if item.kind in ('reply', 'subtask.started', 'subtask.completed')]
    assert len(items) == 3
    assert all(item.agent_lane == 2 and item.agent_label == lane['agent_label'] for item in items)


def backend_events(backend: str) -> list[AgentEvent]:
    if backend.startswith('opencode'):
        return [event for line in OPENCODE_TRACE.splitlines() for event in parse_opencode_line(line)]
    if backend == 'codex':
        trace = Path(__file__).with_name('fixtures').joinpath('codex_turn.jsonl').read_text()
        return [event for line in trace.splitlines() for event in parse_codex_line(line)]
    if backend == 'pi':
        calls = {}
        events = []
        for line in PI_TRACE.splitlines():
            row = json.loads(line)
            if row.get('toolName') == 'edit' and row['type'] == 'tool_execution_end':
                row['result']['details'] = {'diff': '-   1 a-b\n+   1 a+b'}
            events.extend(parse_pi_line(json.dumps(row), calls))
        return events
    if backend == 'dsh':
        # Shapes mirror dsh-headless and test_dsh.py.
        runner = _RunEvents(None)
        return [event for row in [
            {'type': 'text', 'text': 'Checking files'},
            {'type': 'tool_call', 'callId': 'cmd', 'tool': 'pwsh', 'input': {'command': 'Get-Location'}},
            {'type': 'tool_result', 'callId': 'cmd', 'status': 'completed', 'result': 'C:/demo'},
            {'type': 'tool_call', 'callId': 'edit', 'tool': 'edit', 'input': {'file_path': 'demo.py'}},
            {'type': 'tool_result', 'callId': 'edit', 'status': 'completed', 'result': 'Edited'},
            {'type': 'tool_call', 'callId': 'read', 'tool': 'read', 'input': {'file_path': 'demo.py'}},
            {'type': 'tool_result', 'callId': 'read', 'status': 'completed', 'result': 'Contents'},
        ] if (event := runner.consume(row)) is not None]
    return [
        AgentEvent(type='command', data={'command': 'pwd', 'exit_code': 0, 'output_tail': '/workspace\n'}),
        AgentEvent(type='message', data={'text': 'Fake turn completed.'}),
        AgentEvent(type='turn_completed', data={'final_message': 'Fake turn completed.'}),
    ]


@pytest.mark.parametrize(('backend', 'exit_code', 'file_name', 'counts'), [
    ('codex', 0, 'calc.py', (None, None)),
    ('opencode', 0, 'stats.py', (1, 1)),
    ('opencode-brain', 0, 'stats.py', (1, 1)),
    ('pi', None, 'app.py', (1, 1)),
    ('dsh', None, 'demo.py', (None, None)),
    ('fake', 0, None, None),
])
async def test_normalizes_backend_payloads(backend, exit_code, file_name, counts):
    task, events = history(backend, [(f'agent.{event.type}', event.data) for event in backend_events(backend)])
    page = await project(task, events)
    assert any(item.kind == 'reply' and item.backend == backend and item.scope == 'compile' for item in page.items)
    activities = [item.activity for item in page.items if item.activity]
    commands = [op for group in activities for op in group.operations if op.kind == 'command']
    assert commands and all(command.exit_code == exit_code and command.output_tail for command in commands)
    edits = [edit for group in activities for edit in group.files]
    if file_name:
        [edit] = edits
        assert edit.path.endswith(file_name)
        assert (edit.additions, edit.deletions) == counts
    else:
        assert not edits
    if backend == 'dsh':
        [read] = [op for group in activities for op in group.operations if op.kind == 'tool']
        assert read.name == 'read' and read.summary == 'file_path=demo.py'
    if backend == 'fake':
        assert [item.kind for item in page.items].count('reply') == 1


async def test_groups_turn_actions_and_filters_noise_across_cursor_pages():
    command = {'command': 'pwd', 'exit_code': 0, 'call_id': 'cmd', 'status': 'completed'}
    task, events = history('dsh', [
        ('agent.tool', {'name': 'pwsh', 'call_id': 'cmd', 'status': 'started'}),
        ('agent.command', command),
        ('agent.usage', {'output_tokens': 5}),
        ('agent.file_change', {'changes': [{'path': 'a.py', 'additions': 2}]}),
        ('agent.command', command),
        ('agent.file_change', {'changes': [{'path': 'a.py', 'additions': 1, 'deletions': 2}]}),
        ('agent.message', {'text': 'streamed', 'status': 'delta'}),
        ('agent.message', {'text': 'Done'}),
        ('agent.message', {'text': 'Done'}),
        ('agent.turn_completed', {'final_message': 'Done'}),
        ('agent.turn_started', {'turn': 2, 'backend': 'dsh', 'subtask': 'test'}),
        ('agent.command', {**command, 'turn': 2, 'subtask': 'test'}),
        ('agent.turn_completed', {'turn': 2, 'subtask': 'test', 'final_message': 'Final without text event'}),
        ('agent.error', {'subtask': 'test', 'turn': 2, 'message': 'Error'}),
    ])
    page = await project(task, events)
    assert [item.kind for item in page.items] == [
        'agent.turn_started', 'operations', 'reply', 'agent.turn_completed', 'agent.turn_started', 'operations',
        'reply', 'agent.turn_completed', 'error',
    ]
    group = page.items[1].activity
    assert len(group.operations) == 1
    assert [edit.model_dump() for edit in group.files] == [{'path': 'a.py', 'additions': 3, 'deletions': 2}]
    assert page.items[5].activity.turn_seq != group.turn_seq

    cursor, continued = 0, []
    while True:
        chunk = await project(task, events, after=cursor, limit=2)
        continued.extend(chunk.items)
        cursor = int(chunk.cursor)
        if not chunk.has_more:
            break
    assert [item.text for item in continued if item.kind == 'reply'] == ['Done', 'Final without text event']
    activities = [item.activity for item in continued if item.activity]
    assert len({part.turn_seq for part in activities}) == 2
    assert sum(len(part.operations) for part in activities) == 2


async def test_caps_and_redacts_agent_payloads_before_display():
    note = 'x' * 9000
    sensitive = 'owner@example.org'
    task, events = history('pi', [
        ('agent.message', {'text': f'**Done** {sensitive} {note}', 'raw': 'fixture-debug-json'}),
        ('agent.command', {'command': f'echo {sensitive}; API_KEY=fixture-credential',
                           'output_tail': note + f'\nContact {sensitive}', 'exit_code': 9}),
        ('agent.file_change', {'changes': [{'path': f'/work/{sensitive}/app.py', 'patch': '-x\n+y'}]}),
        ('agent.tool', {'name': 'search', 'input': {'path': sensitive, 'token': 'fixture-credential',
                                                   'content': 'fixture-unpublished-content'}}),
        ('agent.error', {'message': 'Authorization: Bearer fixture-credential'}),
    ])
    page = await project(task, events, user=GuestPrincipal(tenant_id='team'))
    reply = next(item for item in page.items if item.kind == 'reply')
    activity = next(item.activity for item in page.items if item.activity)
    assert reply.text.startswith('**Done** [redacted]') and len(reply.text) == 8000
    assert '[redacted]' in activity.files[0].path
    assert activity.operations[1].summary == 'path=[redacted]'
    assert len(activity.operations[0].output_tail) == 1600
    serialized = page.model_dump_json()
    for needle in (sensitive, 'fixture-credential', 'fixture-debug-json', 'fixture-unpublished-content'):
        assert needle not in serialized
    member = (await project(task, events)).model_dump_json()
    assert sensitive in member and 'fixture-credential' not in member
