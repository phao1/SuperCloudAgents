"""Manual, budgeted two-model coding-basic comparison."""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from temporalio.client import Client

from sca.eval.compare import compare, markdown
from sca.eval.workflow import EvalRunWorkflow

from . import test_eval_runner
from .test_e2e_evaluation import require_docker  # noqa: F401


@pytest.fixture
async def gateway_stack(stack, monkeypatch):
    return await test_eval_runner.eval_http.__wrapped__(stack, monkeypatch)

pytestmark = [pytest.mark.e2e, pytest.mark.gateway]


@pytest.fixture
def agent_backend() -> str:
    return 'codex'


@pytest.fixture(autouse=True)
def require_models():
    if len(os.environ.get('SCA_EVAL_MODELS', '').split(',')) != 2:
        pytest.skip('Set SCA_EVAL_MODELS to two gateway model aliases for this paid run')


async def test_two_models_coding_basic(gateway_stack, capsys):
    stack = gateway_stack
    if not stack.settings.model_gateway_url:
        pytest.skip('Model gateway unconfigured')
    client = await Client.connect(stack.settings.temporal_address)
    runs = []
    for model in os.environ['SCA_EVAL_MODELS'].split(','):
        run_id = str(uuid4())
        request = {'id': run_id, 'suite': str(Path('evals/coding-basic').resolve()),
                   'variant': {'model': model.strip(), 'agent_backend': 'codex',
                               'prompt_version': 'v1', 'memory_enabled': False},
                   'repeat': 1, 'concurrency': 1, 'budget': .6, 'case_budget': .025}
        handle = await client.start_workflow(EvalRunWorkflow.run, request, id=f'eval-{run_id}',
                                             task_queue=stack.settings.task_queue)
        async with asyncio.timeout(2400):
            assert await handle.result() == 'completed'
        run = await stack.store.get_eval_run(run_id)
        runs.append(run)
        stack.submitted.extend(str(row['task_id']) for row in run['results'])
    with capsys.disabled():
        print(markdown(compare(*runs)))
