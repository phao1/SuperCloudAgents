import asyncio

import pytest
from sandbox_contract import (  # noqa: F401
    sandbox_fixture,
    test_archive_empty_directory,
    test_archive_large_file,
    test_archive_missing_path,
    test_archive_round_trip,
    test_create_destroy,
    test_env_network_mounts_ownership,
    test_network_none,
    test_stream_stdin_long_line_exit,
    test_timeout_and_close,
)

from sca.sandbox.docker import DockerSandboxProvider

pytestmark = pytest.mark.integration


@pytest.fixture
async def provider():
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.wait(), 10)
    except (OSError, TimeoutError):
        pytest.skip("Docker is unreachable")
    if proc.returncode:
        pytest.skip("Docker is unreachable")
    return DockerSandboxProvider()


async def test_recreate_stopped(sandbox):
    provider, sid, name, spec = sandbox
    await provider._cli("stop", sid)
    assert await provider.create(name, spec) == sid
