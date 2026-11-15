"""Three real scripted-agent cases, repeated and resumed on a replacement worker."""

import asyncio
import os
import socket
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
import yaml
from temporalio.client import Client

from sca.api.auth import StaticTokenAuthenticator
from sca.eval.activities import EvalActivities
from sca.eval.workflow import EvalRunWorkflow

from .test_e2e_evaluation import require_docker  # noqa: F401

pytestmark = pytest.mark.e2e


@pytest.fixture
def in_process_worker() -> bool:
    return False


def scripted_suite(root: Path) -> None:
    root.mkdir()
    (root / 'suite.yaml').write_text('name: scripted-three\nkind: coding\n')
    for name in ('one', 'two', 'wrong'):
        case = root / 'cases' / name
        (case / 'repo').mkdir(parents=True)
        (case / 'acceptance').mkdir()
        (case / 'repo/app.py').write_text('value = 0\n')
        (case / 'acceptance/hidden.py').write_text('from app import value\nassert value == 42\n')
        (case / 'acceptance.yaml').write_text('command: python hidden.py\n')
        value = 0 if name == 'wrong' else 42
        (case / 'task.yaml').write_text(yaml.safe_dump({
            'instruction': f'Update value.\n```sca-fake\nsleep 2; printf "value = {value}\\n" > app.py\n```',
            'verify_command': 'python -m py_compile app.py', 'max_fix_attempts': 0,
        }))


@pytest.fixture
async def eval_http(stack, monkeypatch):
    stack.http._transport.app.state.authenticator = StaticTokenAuthenticator({'e2e-token': 'eval'})
    def http(self):
        return httpx.AsyncClient(transport=stack.http._transport, base_url='http://sca.test',
                                 headers={'Authorization': 'Bearer e2e-token'})
    monkeypatch.setattr(EvalActivities, 'http', http)
    return stack


async def test_scripted_suite_resume(eval_http, tmp_path):
    stack = eval_http
    suite = tmp_path / 'suite'
    scripted_suite(suite)
    client = await Client.connect(stack.settings.temporal_address)
    run_id = str(uuid4())
    request = {'id': run_id, 'suite': str(suite), 'variant': {'agent_backend': 'fake', 'memory_enabled': False},
               'repeat': 2, 'concurrency': 2, 'budget': 1, 'case_budget': .1}
    app = stack.http._transport.app
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', lifespan='off'))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    async with asyncio.timeout(10):
        while not server.started:
            await asyncio.sleep(.05)

    def spawn():
        settings = stack.settings
        env = {**os.environ, 'SCA_DATA_DIR': str(settings.data_dir),
               'SCA_DATABASE_URL': settings.database_url,
               'SCA_TEMPORAL_ADDRESS': settings.temporal_address,
               'SCA_TASK_QUEUE': settings.task_queue, 'SCA_AGENT_BACKEND': 'fake',
               'SCA_STATE_TRANSPORT': settings.state_transport,
               'SCA_MAINTENANCE_SCHEDULE': 'false', 'SCA_TURN_HEARTBEAT_TIMEOUT_S': '8',
               'SCA_API_URL': f'http://127.0.0.1:{port}', 'SCA_EVAL_API_TOKEN': 'e2e-token'}
        return subprocess.Popen([sys.executable, '-m', 'sca.orchestration.worker'], env=env)

    first, second = spawn(), None
    try:
        handle = await client.start_workflow(EvalRunWorkflow.run, request, id=f'eval-{run_id}',
                                             task_queue=stack.settings.task_queue)
        async with asyncio.timeout(120):
            while True:
                from sqlalchemy.exc import NoResultFound
                try:
                    run = await stack.store.get_eval_run(run_id)
                except NoResultFound:
                    await asyncio.sleep(.1)
                    continue
                if run['results']:
                    assert len(run['results']) < 6
                    break
                await asyncio.sleep(.1)
        first.kill()
        await asyncio.to_thread(first.wait, timeout=30)
        second = spawn()
        async with asyncio.timeout(240):
            assert await handle.result() == 'completed'
        run = await stack.store.get_eval_run(run_id)
        assert len(run['results']) == 6
        assert sum(row['acceptance_passed'] for row in run['results']) == 4
        assert {row['repeat'] for row in run['results']} == {0, 1}
        timeline = []
        for row in run['results']:
            task_id = str(row['task_id'])
            stack.submitted.append(task_id)
            events = await stack.events(task_id)
            timeline += [(event['created_at'], 1) for event in events if event['type'] == 'task.created']
            timeline += [(event['created_at'], -1) for event in events
                         if event['type'] == 'task.state_changed'
                         and event['payload']['to'] in {'SUCCEEDED', 'FAILED', 'CANCELLED'}]
        active, maximum = 0, 0
        for _, delta in sorted(timeline):
            active += delta
            maximum = max(maximum, active)
        assert maximum <= 2 and active == 0
    finally:
        for process in (first, second):
            if process and process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=30)
        server.should_exit = True
        await server_task
        listener.close()
