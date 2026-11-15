import pytest
from sandbox_contract import (  # noqa: F401
    sandbox_fixture,
    test_create_destroy,
    test_env_network_mounts_ownership,
    test_network_none,
    test_stream_stdin_long_line_exit,
    test_timeout_and_close,
)

from sca.sandbox.docker import DockerSandboxProvider

pytestmark = pytest.mark.integration


@pytest.fixture
def provider():
    return DockerSandboxProvider()


async def test_recreate_stopped(sandbox):
    provider, sid, name, spec = sandbox
    await provider._cli("stop", sid)
    assert await provider.create(name, spec) == sid
