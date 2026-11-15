import httpx
import pytest

from sca.api.app import create_app
from sca.api.auth import ChainAuthenticator, DatabaseTokenAuthenticator, StaticTokenAuthenticator
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


class CallbackRecorder:
    def __init__(self):
        self.ci = []

    async def submit_ci_result(self, task_id, commit, passed, summary, url):
        self.ci.append((task_id, commit, passed, summary, url))


async def test_ci_token_is_scoped_to_callback_repository_and_tenant(store, api_store, tmp_path):
    orchestrator = CallbackRecorder()
    app = create_app(
        store=api_store, orchestrator=orchestrator, object_store=LocalObjectStore(tmp_path),
        authenticator=ChainAuthenticator([
            DatabaseTokenAuthenticator(store), StaticTokenAuthenticator({'admin': 'one'}),
        ]),
    )
    tasks = []
    for tenant, repo in [('one', '/tmp/repo'), ('one', '/tmp/other'), ('two', '/tmp/repo')]:
        task, _ = await store.create_task(
            tenant_id=tenant, kind='coding', spec={'repo_url': repo, 'instruction': 'fix'},
            idempotency_key=None,
        )
        tasks.append(task)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url='http://test',
    ) as client:
        response = await client.post('/v1/tokens', headers={'Authorization': 'Bearer admin'},
            json={'name': 'ci', 'scopes': ['ci:report'], 'repo_url': '/tmp/repo'})
        assert response.status_code == 201
        headers = {'Authorization': 'Bearer ' + response.json()['token']}
        assert (await client.get('/v1/tasks', headers=headers)).status_code == 403
        payload = {'commit': 'a' * 40, 'passed': True, 'summary': 'ok'}
        for task, expected in zip(tasks, [200, 403, 404], strict=True):
            result = await client.post(f'/v1/tasks/{task.id}/ci-result',
                                       headers=headers, json=payload)
            assert result.status_code == expected
        assert len(orchestrator.ci) == 1
        assert orchestrator.ci[0][:3] == (str(tasks[0].id), 'a' * 40, True)
        token_id = response.json()['id']
        assert (await client.delete(f'/v1/tokens/{token_id}',
                                   headers={'Authorization': 'Bearer admin'})).status_code == 204
        assert (await client.post(f'/v1/tasks/{tasks[0].id}/ci-result',
                                  headers=headers, json=payload)).status_code == 401
