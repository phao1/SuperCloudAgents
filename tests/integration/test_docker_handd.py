import os

import pytest

from sca.core.models import ExecExit, SandboxSpec
from sca.sandbox.docker import DockerSandboxProvider

pytestmark = [pytest.mark.integration, pytest.mark.docker,
              pytest.mark.skipif(os.getenv('SCA_TEST_DOCKER_HANDD') != '1',
                                 reason='opt-in Docker handd test')]


async def test_handd_in_sandbox():
    provider = DockerSandboxProvider()
    provider._handd_enabled = True
    sandbox = await provider.create('handd-test', SandboxSpec(
        image=os.getenv('SCA_TEST_DOCKER_IMAGE', 'sca-sandbox:latest'), network='none'))
    try:
        assert [e async for e in provider.exec(sandbox, ['true'])] == [ExecExit(exit_code=0)]
        client = provider._handd_clients[sandbox]
        assert client.proc.returncode is None
        assert [e async for e in provider.exec(sandbox, ['true'])] == [ExecExit(exit_code=0)]
    finally:
        for client in provider._handd_clients.values():
            await client.close()
        await provider.destroy(sandbox)
