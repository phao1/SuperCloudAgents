import os
from uuid import uuid4

import pytest

from sca.core.models import ExecExit, SandboxSpec
from sca.sandbox.docker import DockerSandboxProvider

pytestmark = [pytest.mark.docker, pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("SCA_TEST_DOCKER_SUSPEND") != "1",
                                 reason="opt in with SCA_TEST_DOCKER_SUSPEND=1")]


async def test_freeze_exec_and_remove():
    provider = DockerSandboxProvider()
    sandbox = await provider.create("suspend-test-" + uuid4().hex, SandboxSpec(
        image=os.environ.get("SCA_TEST_DOCKER_IMAGE", "sca-sandbox:latest"), network="none"))
    try:
        await provider.pause(sandbox)
        await provider.pause(sandbox)
        _, state, _ = await provider._cli("inspect", "--format", "{{.State.Paused}}", sandbox)
        assert state.strip() == "true"
        events = [event async for event in provider.exec(sandbox, ["true"], timeout_s=10)]
        assert events[-1] == ExecExit(exit_code=0)
        await provider.pause(sandbox)
    finally:
        await provider.destroy(sandbox)
    assert not await provider.exists(sandbox)
