from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sandbox_contract import (  # noqa: F401
    sandbox_fixture,
    test_create_destroy,
    test_env_network_mounts_ownership,
    test_network_none,
    test_stream_stdin_long_line_exit,
    test_timeout_and_close,
)
from test_egress import (  # noqa: F401
    test_allowlisted_host_is_reachable,
    test_no_route_out_without_the_proxy,
    test_other_hosts_are_refused_by_the_proxy,
    test_proxy_refuses_private_destinations_even_if_allowlisted,
)

from sca.core.errors import SandboxError
from sca.core.models import SandboxSpec
from sca.sandbox.k8s import K8sSandboxProvider, pod_manifest

pytestmark = [pytest.mark.integration, pytest.mark.k8s]


@pytest_asyncio.fixture
async def provider():
    provider = K8sSandboxProvider(
        str(Path.home() / ".local/bin/kubectl"),
        context="kind-sca",
        namespace=f"sca-test-{uuid4().hex[:12]}",
        egress_allowlist=["pypi.org", "172.17.0.1"],
    )
    try:
        code, _, err = await provider._cli("get", "nodes", "--request-timeout=5s", check=False)
    except SandboxError as exc:
        pytest.skip(f"kind-sca unavailable: {exc}")
    if code:
        pytest.skip(f"kind-sca unavailable: {err}")
    try:
        yield provider
    finally:
        await provider._cli(
            "delete", "namespace", provider.namespace, "--ignore-not-found", "--wait=false"
        )


@pytest_asyncio.fixture
async def restricted(provider):
    sid = await provider.create(
        "restricted", SandboxSpec(image="sca-sandbox:latest", cpus=0.1, memory_mb=128)
    )
    yield provider, sid


async def test_recreate_stopped(provider):
    spec = SandboxSpec(image="sca-sandbox:latest", network="none", cpus=0.1, memory_mb=128)
    manifest = pod_manifest("stopped", spec, namespace=provider.namespace)
    manifest["spec"]["containers"][0]["command"] = ["sh", "-c", "exit 0"]
    await provider._namespace()
    await provider._apply([manifest])
    sid = manifest["metadata"]["name"]
    await provider._cli(
        "wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{sid}", "--timeout=180s"
    )
    assert await provider.create("stopped", spec) == sid


@pytest.mark.xfail(
    strict=False,
    reason="kind's built-in CNI does not filter traffic to the API server on the pod's own node; "
    "a production CNI (Calico, Cilium) is needed for this. The pod has no service account "
    "token, so what it reaches is the unauthenticated API surface only.",
)
async def test_none_cannot_reach_cluster_api(sandbox):
    provider, sid, *_ = sandbox
    _, address, _ = await provider._cli(
        "get",
        "service",
        "kubernetes",
        "--namespace",
        "default",
        "-o",
        "jsonpath={.spec.clusterIP}",
    )
    from sandbox_contract import collect

    result = await collect(
        provider,
        sid,
        [
            "python3",
            "-c",
            "import socket,sys; socket.create_connection((sys.argv[1], 443), timeout=3)",
            address,
        ],
        timeout_s=10,
    )
    assert result[-1].exit_code != 0
