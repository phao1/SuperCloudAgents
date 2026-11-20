"""Real Temporal orchestration with fake eval children and I/O activities."""

import asyncio
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from sca.config import get_settings
from sca.evolve.models import EvolutionConfig
from sca.evolve.workflow import EvolutionWorkflow

pytestmark = pytest.mark.integration


@workflow.defn(name='EvalRunWorkflow')
class FakeEval:
    @workflow.run
    async def run(self, request: dict) -> str:
        return 'completed'


@pytest.fixture
async def temporal_env():
    try:
        async with asyncio.timeout(3):
            client = await Client.connect(get_settings().temporal_address)
    except (RuntimeError, OSError, TimeoutError) as exc:
        pytest.skip(f'Temporal unavailable: {exc}')
    async with WorkflowEnvironment.from_client(client) as env:
        yield env


@pytest.mark.parametrize('scenario,expected,promotions', [
    ('promotion', 'completed', 1),
    ('rejection', 'completed', 0),
    ('budget', 'budget_exhausted', 0),
    ('patience', 'patience_exhausted', 0),
    ('approval', 'completed', 1),
    ('reject_approval', 'completed', 0),
    ('stop', 'stopped', 0),
])
async def test_evolution_paths(temporal_env, scenario, expected, promotions):
    runs, records, statuses, promoted = {}, {}, [], []
    waiting = asyncio.Event()
    cfg = EvolutionConfig(suite='test', agent_version='v', generations=3 if scenario == 'patience' else 1,
                          candidates=1, repeat=1, patience=2,
                          budget_usd=.1 if scenario == 'budget' else 10,
                          require_approval=scenario in ('approval', 'reject_approval', 'stop')).model_dump()

    @activity.defn(name='evolve_initialize')
    async def initialize(request: dict) -> dict:
        return {'id': None, 'candidate_id': None, 'overlay': {}}

    @activity.defn(name='evolve_prepare_run')
    async def prepare(request: dict) -> dict:
        runs[request['run_id']] = request
        return {'id': request['run_id']}

    @activity.defn(name='evolve_read_run')
    async def read(request: dict) -> dict:
        item = runs[request['run_id']]
        passed = bool(item['overlay'])
        if scenario in ('rejection', 'patience') and item['split'] == 'holdout':
            passed = not bool(item['overlay'])
        return {'id': request['run_id'], 'status': 'completed', 'suite_hash': 'same',
                'results': [{'case_id': 'c', 'repeat': 0, 'acceptance_passed': passed,
                             'verification_passed': True, 'trajectory_passed': True,
                             'cost_usd': .1, 'turns': 1}]}

    @activity.defn(name='evolve_propose')
    async def propose(request: dict) -> list[dict]:
        return [{'hypothesis': 'verify', 'overlay': {'tool_guidance': 'verify'},
                 'overlay_sha256': 'hash', 'diagnosis': {}, 'static_check': {'valid': True}}]

    @activity.defn(name='evolve_record')
    async def record(request: dict) -> None:
        records[request['candidate']['id']] = request['candidate']

    @activity.defn(name='evolve_status')
    async def status(request: dict) -> None:
        statuses.append(request)
        if request['status'] == 'awaiting_approval':
            waiting.set()

    @activity.defn(name='evolve_promote')
    async def promote(request: dict) -> dict:
        promoted.append(request)
        return {'id': request['champion_id'], 'candidate_id': request['candidate']['id'],
                'overlay': request['candidate']['overlay']}

    queue = f'evolve-test-{uuid4()}'
    async with Worker(temporal_env.client, task_queue=queue, workflows=[EvolutionWorkflow, FakeEval],
                      activities=[initialize, prepare, read, propose, record, status, promote]):
        async with asyncio.timeout(45):
            handle = await temporal_env.client.start_workflow(EvolutionWorkflow.run,
                {'id': str(uuid4()), 'config': cfg}, id=f'evolve-{uuid4()}', task_queue=queue)
            if cfg['require_approval']:
                await waiting.wait()
                pending = next(c for c in records.values() if c['reason'] == 'awaiting approval')
                await handle.signal('approve', 'stale-candidate')
                if scenario == 'stop':
                    await handle.signal('stop')
                else:
                    await handle.signal('reject' if scenario == 'reject_approval' else 'approve', pending['id'])
            assert await handle.result() == expected
    assert len(promoted) == promotions
    assert statuses[-1]['status'] == expected
    assert statuses[-1]['spent_usd'] <= cfg['budget_usd']
    if scenario == 'patience':
        assert len(records) == 2
        assert sum(r['split'] == 'dev' and not r['overlay'] for r in runs.values()) == 1
    assert all(c['decision'] != 'pending' for c in records.values())
