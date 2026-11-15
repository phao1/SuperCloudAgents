"""Temporal scheduling and restart using deterministic scripted activities."""

import asyncio
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from sca.config import get_settings
from sca.eval.workflow import EvalRunWorkflow

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('budget,expected_count', [(10, 6), (.2, 2)])
async def test_eval_repeats_concurrency_and_worker_resume(budget, expected_count):
    settings = get_settings()
    try:
        client = await Client.connect(settings.temporal_address)
    except (RuntimeError, OSError) as exc:
        pytest.skip(f'Temporal unavailable: {exc}')
    queue = f'eval-test-{uuid4()}'
    submitted, completed, statuses = {}, {}, []
    paused = True
    maximum = 0

    @activity.defn(name='eval_prepare')
    async def prepare(request: dict) -> dict:
        return {'cases': [{'id': c} for c in ('a', 'b', 'wrong')]}

    @activity.defn(name='eval_submit')
    async def submit(request: dict) -> str:
        nonlocal maximum
        key = f"{request['case']['id']}:{request['iteration']}"
        submitted[key] = request
        maximum = max(maximum, len(submitted.keys() - completed.keys()))
        return key

    @activity.defn(name='eval_collect')
    async def collect(request: dict) -> float | None:
        if paused:
            return None
        completed[request['task_id']] = request['case']['id'] != 'wrong'
        return .1

    @activity.defn(name='eval_finish')
    async def finish(request: dict) -> None:
        statuses.append(request['status'])

    def worker():
        return Worker(client, task_queue=queue, workflows=[EvalRunWorkflow],
                      activities=[prepare, submit, collect, finish])

    request = {'id': str(uuid4()), 'suite': 'test', 'repeat': 2, 'concurrency': 2,
               'budget': budget, 'case_budget': .1}
    first = worker()
    async with first:
        handle = await client.start_workflow(EvalRunWorkflow.run, request,
                                             id=f'eval-{uuid4()}', task_queue=queue)
        async with asyncio.timeout(20):
            while len(submitted) < 2:
                await asyncio.sleep(.05)
    # Nothing in workflow memory is required by the replacement worker.
    paused = False
    async with worker():
        async with asyncio.timeout(30):
            result = await handle.result()
    assert len(completed) == expected_count
    assert maximum <= 2
    assert len(submitted) == expected_count
    assert result == ('completed' if expected_count == 6 else 'budget_exhausted')
    assert statuses == [result]
    if expected_count == 6:
        assert sum(completed.values()) == 4
        assert len(completed) - sum(completed.values()) == 2
