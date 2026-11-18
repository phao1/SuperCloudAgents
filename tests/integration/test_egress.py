"""A restricted sandbox can reach allowlisted hosts through the proxy and nothing else."""

from __future__ import annotations

import os
import uuid

import pytest

from sca.core.models import ExecExit, SandboxSpec
from sca.sandbox.docker import DockerSandboxProvider

pytestmark = [pytest.mark.integration, pytest.mark.docker,
              pytest.mark.skipif(os.getenv("SCA_TEST_EGRESS") != "1", reason="opt-in Docker test")]


@pytest.fixture
async def restricted():
    prefix = f"scatest{uuid.uuid4().hex[:8]}"
    provider = DockerSandboxProvider(
        egress_allowlist=["pypi.org"], name_prefix=prefix
    )
    sandbox_id = await provider.create(
        f"egress-{prefix}", SandboxSpec(image="sca-sandbox:latest", network="restricted")
    )
    try:
        yield provider, sandbox_id
    finally:
        await provider.destroy(sandbox_id)
        await provider._cli("rm", "-f", provider.proxy_name, check=False)
        await provider._cli("network", "rm", provider.network_name, check=False)


async def sh(provider, sandbox_id, script: str) -> tuple[int, str]:
    lines, code = [], -1
    async for ev in provider.exec(sandbox_id, ["bash", "-c", script], timeout_s=40):
        if isinstance(ev, ExecExit):
            code = ev.exit_code
        else:
            lines.append(ev.text)
    return code, "\n".join(lines)


async def test_allowlisted_host_is_reachable(restricted):
    provider, sid = restricted
    code, out = await sh(provider, sid, "curl -sS -m 20 -o /dev/null -w '%{http_code}' https://pypi.org/simple/pip/")
    assert (code, out) == (0, "200")


async def test_other_hosts_are_refused_by_the_proxy(restricted):
    provider, sid = restricted
    # With TLS inspection Squid may answer the denial inside the bumped session, so curl can
    # exit 0; what matters is that the response is the proxy's 403, not the site.
    _, out = await sh(provider, sid,
                         "curl -sS -m 20 -o /dev/null -w '%{http_code}' https://example.com/ 2>&1")
    assert "403" in out and "200" not in out


async def test_no_route_out_without_the_proxy(restricted):
    provider, sid = restricted
    code, _ = await sh(provider, sid, "curl -sS -m 5 --noproxy '*' -o /dev/null https://1.1.1.1/")
    assert code != 0
    # The Docker host (and services published on it, such as Postgres) is out of reach too.
    code, _ = await sh(provider, sid, "curl -sS -m 5 --noproxy '*' -o /dev/null http://172.17.0.1:5432/")
    assert code != 0


async def test_proxy_refuses_private_destinations(restricted):
    provider, sid = restricted
    # IP literals and private ranges are refused before any allow rule.
    _, out = await sh(
        provider, sid, "curl -sS -m 10 -o /dev/null -w '%{http_code}' http://172.17.0.1/ 2>&1"
    )
    assert "403" in out
