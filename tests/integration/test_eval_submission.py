"""The runner submits through authenticated public APIs with stable retry keys."""

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.eval.activities import EvalActivities
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("repeat", [1, 2])
@pytest.mark.parametrize("kind", ["coding", "analysis"])
async def test_eval_api_submission_retry_and_analysis_inputs(store, tmp_path, monkeypatch, kind, repeat):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    objects = LocalObjectStore(tmp_path / 'objects')
    app = create_app(store=store, object_store=objects,
                     authenticator=StaticTokenAuthenticator({'eval-test': 'eval'}),
                     orchestrator=SimpleNamespace(start_task=AsyncMock()))
    settings = Settings(data_dir=tmp_path / 'data')
    acts = EvalActivities(settings, store)
    def http():
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                 headers={'Authorization': 'Bearer eval-test'})
    monkeypatch.setattr(acts, 'http', http)
    suite = tmp_path / 'suite'
    (suite / 'cases' / 'one' / 'data').mkdir(parents=True)
    case = suite / 'cases' / 'one'
    (suite / 'suite.yaml').write_text(f'name: api-test\nkind: {kind}\n')
    (case / 'task.yaml').write_text('question: Count rows\n' if kind == 'analysis' else 'instruction: Fix bug\n')
    (case / 'repo').mkdir()
    (case / 'repo' / 'app.py').write_text('value = 0\n')
    (case / 'data' / 'input.csv').write_text('value\n1\n2\n')
    (case / 'acceptance').mkdir()
    (case / 'acceptance' / 'hidden.py').write_text('assert True\n')
    (case / 'acceptance.yaml').write_text('command: python hidden.py\n')
    request = {'id': str(uuid4()), 'suite': str(suite), 'repeat': repeat, 'concurrency': 1,
               'budget': .5, 'case_budget': .1, 'variant': {}}
    env = ActivityEnvironment()
    plan = await env.run(acts.prepare, request)
    submission = {**request, 'case': plan['cases'][0], 'iteration': 0}
    first = await env.run(acts.submit, submission)
    second = await env.run(acts.submit, submission)
    assert first == second
    task = await store.for_tenant('eval').get_task(first)
    assert task.spec['priority'] == 'eval'
    assert task.spec['auto_finish'] is True
    assert task.spec['eval_run_id'] == request['id']
    assert task.spec['memory_enabled'] is False  # eval runs never use shared memory implicitly
    assert task.spec['acceptance_command'] == 'python hidden.py'
    if kind == 'analysis':
        metadata = await store.for_tenant('eval').get_file(task.spec['inputs'][0])
        assert metadata['name'] == 'input.csv'
        assert await objects.get(f"tenants/eval/files/{metadata['id']}") == b'value\n1\n2\n'
    else:
        repo = Path(task.spec['repo_url'])
        assert (repo / 'HEAD').is_file() and (repo / 'objects').is_dir()
        assert not (repo / 'app.py').exists()
    if kind == 'coding' and repeat > 1:
        assert task.spec['labels']['fork_from_snapshot'] == 'true'
        next_id = await env.run(acts.submit, {**submission, 'iteration': 1})
        next_task = await store.for_tenant('eval').get_task(next_id)
        assert next_task.spec['repo_url'] == task.spec['repo_url']
    (case / 'task.yaml').write_text('question: changed\n')
    with pytest.raises(ApplicationError, match='Suite changed') as exc:
        await env.run(acts.submit, submission)
    assert exc.value.type == 'SuiteChanged'
    assert exc.value.non_retryable
