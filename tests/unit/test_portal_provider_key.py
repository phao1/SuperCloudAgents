from types import SimpleNamespace

import pytest
from dotenv import dotenv_values
from fastapi import HTTPException

from sca.api import portal
from sca.gateway.configuration import model_configured


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["docker", "source"])
async def test_provider_key_is_local_opt_in_and_restarts_gateway(tmp_path, monkeypatch, mode):
    env_file = tmp_path / '.env'
    env_file.write_text('DEEPSEEK_API_KEY=REPLACE_ME\nOTHER=keep\n')
    monkeypatch.setenv('SCA_PORTAL_LOCAL_CONFIG', str(env_file))
    monkeypatch.setenv('SCA_RUNTIME_MODE', mode)
    if mode == "docker":
        monkeypatch.setenv('SCA_DOCKER_BIN', '/local/docker')
    else:
        monkeypatch.delenv('SCA_DOCKER_BIN', raising=False)
    commands = []

    class Process:
        returncode = 0

        async def wait(self):
            return 0

    async def create_process(*args, **kwargs):
        commands.append(args)
        return Process()

    monkeypatch.setattr(portal.asyncio, 'create_subprocess_exec', create_process)
    body = portal.ProviderKey(key='sk-test-key-123')
    remote = SimpleNamespace(client=SimpleNamespace(host='192.0.2.1'))
    with pytest.raises(HTTPException) as error:
        await portal.set_provider_key(body, remote, None)
    assert error.value.status_code == 403
    assert dotenv_values(env_file)['DEEPSEEK_API_KEY'] == 'REPLACE_ME'

    local = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'))
    assert model_configured() is False
    result = await portal.set_provider_key(body, local, None)
    assert result == {'saved': True, 'gateway_restarted': True}
    assert model_configured() is True
    assert dotenv_values(env_file) == {'DEEPSEEK_API_KEY': 'sk-test-key-123', 'OTHER': 'keep'}
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert commands == [('bash', str(tmp_path / 'scripts/portal_demo.sh'), 'restart-gateway')]
    assert all('sk-test-key-123' not in arg for arg in commands[0])


def test_provider_key_requires_admin(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.responses import Response

    from sca.api.authz import ForbiddenError
    from sca.core.models import Principal

    monkeypatch.setenv('SCA_PORTAL_LOCAL_CONFIG', str(tmp_path / '.env'))
    monkeypatch.setenv('SCA_DOCKER_BIN', '/local/docker')
    app = FastAPI()
    app.include_router(portal.router)

    class Auth:
        async def authenticate(self, token):
            return Principal(tenant_id='eval', subject='test', role='viewer')

    app.state.authenticator = Auth()
    app.add_exception_handler(ForbiddenError, lambda request, exc: Response(status_code=403))
    with TestClient(app, client=('127.0.0.1', 1234)) as client:
        response = client.post('/v1/portal/provider-key', json={'key': 'sk-test-key-123'},
                               headers={'Authorization': 'Bearer viewer'})
    assert response.status_code == 403
    assert not (tmp_path / '.env').exists()
