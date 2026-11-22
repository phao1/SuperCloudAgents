import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter
from temporalio.worker import Replayer

from sca.eval.workflow import EvalRunWorkflow
from sca.evolve import workflow as module
from sca.evolve.models import EvolutionConfig
from sca.evolve.steering import EvolutionMessageInput
from sca.evolve.workflow import EvolutionWorkflow


@pytest.mark.parametrize('values', [
    {'text': '  '}, {'text': 'test\x00'}, {'mode': 'pause', 'remaining_budget_usd': 2},
    {'mode': 'budget'}, {'mode': 'budget', 'remaining_budget_usd': -1},
    {'mode': 'budget', 'remaining_budget_usd': float('inf')},
])
def test_control_messages_validate_payloads(values):
    with pytest.raises(ValidationError):
        EvolutionMessageInput.model_validate(values)


class FakeChild(asyncio.Future):
    def __init__(self, run_id):
        super().__init__()
        self.run_id = run_id
        self.signals = []

    async def signal(self, signal_name):
        self.signals.append(signal_name)
        self.set_result('cancelled')


class ControllerHarness:
    def __init__(self, monkeypatch, **options):
        self.controller = EvolutionWorkflow()
        self.config = EvolutionConfig(suite='fixture', agent_backend='dsh', repeat=1,
                                      patience=5, **options).model_dump()
        self.runs, self.children, self.requests, self.journal = {}, [], [], []
        self.proposals = [{'hypothesis': 'Match boundaries', 'overlay': {'tool_guidance': 'Verify'},
                           'static_check': {'valid': False}}]
        self.on_activity = lambda name, request: None
        self.on_wait = lambda: None
        self.on_child = lambda child: child.set_result('completed')
        monkeypatch.setattr(module.workflow, 'patched', lambda _: True)
        monkeypatch.setattr(module.workflow, 'uuid4', uuid4)
        monkeypatch.setattr(module.workflow, 'execute_activity', self.execute)
        monkeypatch.setattr(module.workflow, 'execute_child_workflow', self.execute_child)
        monkeypatch.setattr(module.workflow, 'start_child_workflow', self.start_child)
        monkeypatch.setattr(module.workflow, 'wait_condition', self.wait)

    async def run(self):
        return await self.controller.run({'id': str(uuid4()), 'config': self.config})

    def send(self, id_, mode, **fields):
        self.controller.user_message({'id': id_, 'mode': mode, **fields})

    async def execute(self, name, request, **_):
        self.requests.append((name, request))
        self.on_activity(name, request)
        match name:
            case 'evolve_initialize':
                return {'id': None, 'overlay': {}}
            case 'evolve_prepare_run':
                self.runs[request['run_id']] = request
                return {'id': request['run_id']}
            case 'evolve_read_run':
                return {'id': request['run_id'], 'status': 'completed', 'suite_hash': 'fixture',
                        'results': [{'case_id': 'case', 'repeat': 0, 'acceptance_passed': False,
                                     'cost_usd': .05, 'turns': 1}]}
            case 'evolve_propose':
                return self.proposals
            case 'evolve_record' | 'evolve_status':
                return None
            case 'evolve_conversation':
                self.journal.extend(request['items'])
            case 'evolve_conversation_budget':
                remaining = request['message']['remaining_budget_usd']
                return request['spent_usd'] + remaining if remaining <= 1 else None
            case _:
                raise AssertionError(name)

    async def execute_child(self, *_, **__):
        return 'completed'

    async def start_child(self, _, payload, **__):
        child = FakeChild(payload['id'])
        self.children.append(child)
        self.on_child(child)
        return child

    async def wait(self, predicate):
        if not predicate():
            self.on_wait()
        assert predicate()


async def test_guidance_applies_to_next_proposal_and_persists_until_superseded(monkeypatch):
    harness = ControllerHarness(monkeypatch, generations=3, candidates=1)
    seen = []

    def steer(name, request):
        if name == 'evolve_initialize':
            harness.send('superseded', 'guidance', text='First idea', seq=1)
            harness.send('persistent', 'guidance', text='Persistent idea', seq=2)
        elif name == 'evolve_propose':
            seen.append(request['operator_guidance'])
            if len(seen) == 2:
                harness.send('new', 'guidance', text='Next proposal only starts here', seq=3)
                harness.send('persistent', 'guidance', text='Duplicate with the same ID', seq=2)

    harness.on_activity = steer
    assert await harness.run() == 'completed'
    assert seen == ['Persistent idea', 'Persistent idea', 'Next proposal only starts here']
    assert [(item['message_id'], item['status'], item.get('generation')) for item in harness.journal
            if item['kind'] == 'receipt'] == [
        ('superseded', 'superseded', None), ('persistent', 'applied', 1), ('new', 'applied', 3),
    ]
    assert '已应用于第 3 代提议' in {item['text'] for item in harness.journal}


async def test_pause_resume_skip_and_stop_at_generation_boundary(monkeypatch):
    harness = ControllerHarness(monkeypatch, generations=3, candidates=2)
    proposal = {**harness.proposals[0], 'static_check': {'valid': True}}
    harness.proposals = [proposal, {**proposal, 'hypothesis': 'Add one check'}]

    def steer(name, request):
        if name == 'evolve_read_run' and not harness.children:
            harness.send('p', 'pause')
        elif name == 'evolve_conversation_budget' and request['message']['id'] == 'excess':
            assert request['spent_usd'] == .05

    def wait():
        if harness.controller.steering.paused:
            assert len(harness.runs) == 1
            harness.send('excess', 'budget', remaining_budget_usd=3)
            harness.send('adjust', 'budget', remaining_budget_usd=.40)
            harness.send('r', 'resume')
        else:
            harness.send('s', 'skip')
            harness.send('stop', 'stop')

    harness.on_activity, harness.on_wait = steer, wait
    harness.on_child = lambda child: child.set_result('completed') if len(harness.children) > 1 else None
    assert await harness.run() == 'stopped'
    assert [child.signals for child in harness.children] == [['skip_evolution_candidate'], []]
    assert [row['remaining'] for row in harness.runs.values()] == [3, .4, .35]
    decisions = {request['candidate']['id']: request['candidate'] for name, request in harness.requests
                 if name == 'evolve_record'}
    assert next(iter(decisions.values()))['reason'] == '操作者已跳过候选'
    assert {item['text'] for item in harness.journal if item['kind'] == 'receipt'} == {
        '演进已暂停', '演进已恢复', '已跳过当前候选', '已安排本代结束后停止',
    }
    assert [request['evolution_status']['status'] for name, request in harness.requests
            if name == 'evolve_conversation' and 'evolution_status' in request] == ['paused', 'running']


@pytest.mark.parametrize('candidate_run', [True, False])
@pytest.mark.parametrize('unpriced', [True, False])
async def test_candidate_skip_drains_the_active_batch_only(monkeypatch, candidate_run, unpriced):
    evaluation = EvalRunWorkflow()
    submitted, collected = [], []

    async def execute(name, request, **kwargs):
        if name == 'eval_prepare':
            return {'cases': [{'id': case, 'model_unpriced': unpriced} for case in ('one', 'two')]}
        if name == 'eval_submit':
            submitted.append(request['case']['id'])
            return request['case']['id']
        assert name == 'eval_collect'
        collected.append(request['case']['id'])
        evaluation.skip_evolution_candidate()
        return {'cost_usd': 0 if unpriced else .01, 'errored': False}

    monkeypatch.setattr(module.workflow, 'execute_activity', execute)
    monkeypatch.setattr(module.workflow, 'patched', lambda _: True)
    request = {'repeat': 1, 'concurrency': 1, 'case_budget': .1, 'budget': .01 if unpriced else 2,
               'evolution_step': {'candidate_id': 'c' if candidate_run else None}}
    status, _ = await evaluation.evaluate(request, {})
    assert status == ('cancelled' if candidate_run else 'completed')
    assert collected == submitted == (['one'] if candidate_run else ['one', 'two'])


async def test_replay_pre_conversation_history_without_service():
    history = []
    converter = DataConverter.default

    def event(name, **fields):
        result = HistoryEvent(event_id=len(history) + 1, event_type=EventType.Value('EVENT_TYPE_' + name.upper()))
        result.event_time.FromDatetime(datetime(2026, 1, 1, tzinfo=UTC))
        attrs = getattr(result, name + '_event_attributes')
        attrs.CopyFrom(type(attrs)(**fields))
        history.append(result)
        return result.event_id

    def workflow_task():
        scheduled = event('workflow_task_scheduled', task_queue={'name': 'tasks'})
        started = event('workflow_task_started', scheduled_event_id=scheduled)
        return event('workflow_task_completed', scheduled_event_id=scheduled, started_event_id=started)

    request = {'id': str(uuid4()), 'config': EvolutionConfig(suite='fixture', agent_backend='dsh',
                                                          budget_usd=.01).model_dump()}
    event('workflow_execution_started', workflow_type={'name': 'EvolutionWorkflow'}, task_queue={'name': 'tasks'},
          input=await converter.encode_wrapper([request]))
    completed = workflow_task()
    for idx, (name, outcome) in enumerate((('evolve_initialize', {'id': None, 'overlay': {}}),
                                           ('evolve_status', None)), start=1):
        scheduled = event('activity_task_scheduled', activity_type={'name': name}, activity_id=str(idx),
                          task_queue={'name': 'tasks'}, workflow_task_completed_event_id=completed)
        started = event('activity_task_started', scheduled_event_id=scheduled, attempt=1)
        event('activity_task_completed', scheduled_event_id=scheduled, started_event_id=started,
              result=await converter.encode_wrapper([outcome]))
        completed = workflow_task()
    event('workflow_execution_completed', workflow_task_completed_event_id=completed,
          result=await converter.encode_wrapper(['budget_exhausted']))
    await Replayer(workflows=[EvolutionWorkflow]).replay_workflow(WorkflowHistory('evolve-legacy', history))
