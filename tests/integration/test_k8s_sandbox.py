import json
from ipaddress import ip_network
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
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
from test_egress import (  # noqa: F401
    test_allowlisted_host_is_reachable,
    test_no_route_out_without_the_proxy,
    test_other_hosts_are_refused_by_the_proxy,
    test_proxy_refuses_private_destinations,
)

from sca.core.errors import SandboxError
from sca.core.models import SandboxSpec
from sca.sandbox.k8s import K8sSandboxProvider, pod_manifest

pytestmark = [pytest.mark.integration, pytest.mark.k8s]


@pytest_asyncio.fixture
async def provider(tmp_path):
    provider = K8sSandboxProvider(
        str(Path.home() / ".local/bin/kubectl"),
        context="kind-sca",
        namespace=f"sca-test-{uuid4().hex[:12]}",
        egress_allowlist=["pypi.org"],
        egress_ca_dir=tmp_path / "ca",
    )
    try:
        code, _, err = await provider._cli("get", "nodes", "--request-timeout=5s", check=False)
    except SandboxError as exc:
        pytest.skip(f"kind-sca unavailable: {exc}")
    if code:
        pytest.skip(f"kind-sca unavailable: {err}")
    code, _, _ = await provider._cli(
        "get", "daemonset", "calico-node", "--namespace", "kube-system", check=False
    )
    if code == 0:
        provider.network_policy_flavor = "calico"
        _, raw, _ = await provider._cli("get", "nodes", "-o", "json")
        provider.blocked_cidrs = [
            str(ip_network(a["address"]))
            for node in json.loads(raw)["items"]
            for a in node["status"]["addresses"]
            if a["type"] in {"InternalIP", "ExternalIP"}
        ]
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


async def test_none_cannot_reach_cluster_api(sandbox):
    provider, sid, *_ = sandbox
    if provider.network_policy_flavor != "calico":
        pytest.skip("kindnet cannot enforce pod-to-host isolation; requires Calico")
    _, address, _ = await provider._cli(
        "get",
        "service",
        "kubernetes",
        "--namespace",
        "default",
        "-o",
        "jsonpath={.spec.clusterIP}",
    )
    await _assert_unreachable(provider, sid, address, 443)


async def _assert_unreachable(provider, sid, address: str, port: int):
    from sandbox_contract import collect

    result = await collect(
        provider,
        sid,
        [
            "python3",
            "-c",
            (
                "import socket,sys\n"
                "try:\n socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3)\n"
                "except OSError:\n sys.exit(42)\n"
            ),
            address,
            str(port),
        ],
        timeout_s=10,
    )
    assert result[-1].exit_code == 42


@pytest.mark.parametrize("network", ["none", "restricted"])
@pytest.mark.parametrize("destination", ["metadata", "kubelet"])
async def test_sensitive_endpoints_unreachable(provider, network, destination):
    if provider.network_policy_flavor != "calico":
        pytest.skip("host isolation requires Calico")
    sid = await provider.create(
        f"{network}-{destination}",
        SandboxSpec(image="sca-sandbox:latest", network=network, cpus=0.1, memory_mb=128),
    )
    try:
        if destination == "metadata":
            await _assert_unreachable(provider, sid, "169.254.169.254", 80)
        else:
            _, raw, _ = await provider._cli("get", "nodes", "-o", "json")
            addresses = [
                a["address"]
                for node in json.loads(raw)["items"]
                for a in node["status"]["addresses"]
                if a["type"] == "InternalIP"
            ]
            assert addresses
            for address in addresses:
                await _assert_unreachable(provider, sid, address, 10250)
    finally:
        await provider.destroy(sid)


async def test_sync_task_on_worker_node(provider, store, tmp_path):
    """Run the coding activity chain on a worker node without any host mounts."""
    import json

    from temporalio.testing import ActivityEnvironment
    from test_workspace import source_repo

    from sca.agents.fake import FakeBackend
    from sca.config import Settings
    from sca.kinds.coding.activities import CodingActivities, PublishInput, StepInput, TurnInput
    from sca.storage.local import LocalObjectStore

    _, nodes, _ = await provider._cli("get", "nodes", "-o", "json")
    workers = [
        n
        for n in json.loads(nodes)["items"]
        if "node-role.kubernetes.io/control-plane" not in n["metadata"].get("labels", {})
    ]
    if not workers:
        pytest.skip("requires deploy/kind/cluster-2node.yaml")
    provider.node_selector = {"kubernetes.io/hostname": workers[0]["metadata"]["name"]}
    source = await source_repo(tmp_path / "source")
    task, _ = await store.create_task(
        tenant_id="default",
        kind="coding",
        idempotency_key=None,
        spec={
            "repo_url": str(source),
            "instruction": "change hello",
            "verify_command": "grep after hello",
        },
    )
    task_id = str(task.id)
    settings = Settings(
        data_dir=tmp_path / "data",
        state_transport="sync",
        sandbox_network="none",
        sandbox_cpus=0.1,
        sandbox_memory_mb=128,
    )
    activities = CodingActivities(
        settings=settings,
        store=store,
        sandbox=provider,
        agent=FakeBackend(),
        objects=LocalObjectStore(tmp_path / "objects"),
    )
    env = ActivityEnvironment()
    try:
        await env.run(activities.prepare_workspace, task_id)
        sid = await env.run(activities.create_sandbox, task_id)
        await env.run(activities.run_setup, task_id)
        await env.run(
            activities.run_agent_turn,
            TurnInput(
                task_id,
                1,
                "```sca-fake\necho after > hello\n```",
            ),
        )
        await env.run(activities.checkpoint_workspace, StepInput(task_id, 1))
        assert (await env.run(activities.run_verification, StepInput(task_id, 1))).passed
        await env.run(activities.publish, PublishInput(task_id, 1, True))
        _, raw, _ = await provider._cli("get", "pod", sid, "-o", "json")
        pod = json.loads(raw)["spec"]
        assert pod["nodeName"] == workers[0]["metadata"]["name"]
        assert all("hostPath" not in volume for volume in pod["volumes"])
        assert (await store.get_task(task_id)).result["verification_passed"]
    finally:
        await env.run(activities.destroy_sandbox, task_id)


async def test_warm_pool_lifecycle(provider, tmp_path):
    import io
    import tarfile

    from sandbox_contract import collect

    provider.pool_size = 2
    spec = SandboxSpec(
        image="sca-sandbox:latest",
        cpus=0.1,
        memory_mb=128,
        labels={"sca.state_transport": "sync", "sca.task_id": str(uuid4())},
        env={"SCA_POOL_TEST": "restored"},
    )
    await provider.replenish(spec, 2)
    idle = await provider.list({"sca.state": "idle"})
    assert len(idle) == 2
    sid = await provider.create("pooled", spec)
    assert sid in idle
    assert await provider.create("pooled", spec) == sid
    _, raw, _ = await provider._cli("get", "pod", sid, "-o", "json")
    pod = json.loads(raw)
    assert pod["metadata"]["labels"]["sca.state"] == "claimed"
    assert "SCA_POOL_TEST" not in {e["name"] for e in pod["spec"]["containers"][0]["env"]}
    # A fresh provider must recover defaults without a create call.
    fresh = K8sSandboxProvider(
        provider.kubectl_bin, context=provider.context, namespace=provider.namespace
    )
    result = await collect(fresh, sid, ["sh", "-c", 'echo "$SCA_POOL_TEST"'])
    assert any(getattr(e, "text", "") == "restored" for e in result)
    result = await collect(
        fresh, sid, ["sh", "-c", 'echo "$SCA_POOL_TEST"'], env={"SCA_POOL_TEST": "override"}
    )
    assert any(getattr(e, "text", "") == "override" for e in result)
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        item = tarfile.TarInfo("hello")
        item.size = 5
        archive.addfile(item, io.BytesIO(b"hello"))

    async def chunks():
        yield data.getvalue()

    await fresh.upload(sid, "/workspace", chunks())
    downloaded = b"".join([chunk async for chunk in fresh.download(sid, "/workspace/hello")])
    with tarfile.open(fileobj=io.BytesIO(downloaded)) as archive:
        assert archive.extractfile("hello").read() == b"hello"
    await provider.destroy(sid)
    await provider.replenish(spec, 2)
    assert len(await provider.list({"sca.state": "idle"})) == 2
    assert not await provider.exists(sid)


async def test_pool_concurrent_claims(provider):
    import asyncio

    provider.pool_size = 2
    spec = SandboxSpec(
        image="sca-sandbox:latest",
        network="none",
        cpus=0.1,
        memory_mb=128,
        labels={"sca.state_transport": "sync"},
    )
    await provider.replenish(spec, 2)
    idle = set(await provider.list({"sca.state": "idle"}))
    first, second = await asyncio.gather(
        provider.create("first", spec), provider.create("second", spec)
    )
    assert first != second
    assert {first, second} == idle


async def test_tls_inspection_and_public_bundle(restricted):
    from sandbox_contract import collect
    from test_egress_tls import PROBE

    from sca.sandbox.egress import CA_ENV, CA_TARGET

    provider, sid = restricted
    _, raw, _ = await provider._cli("get", "pod", sid, "-o", "json")
    pod = json.loads(raw)
    assert all("secret" not in v for v in pod["spec"]["volumes"])
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert all(env[key] == CA_TARGET for key in CA_ENV)
    address = env["HTTPS_PROXY"].removeprefix("http://").split(":")[0]
    result = await collect(provider, sid, ["python3", "-c", PROBE, address], timeout_s=150)
    assert result[-1].exit_code == 0
