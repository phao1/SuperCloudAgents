import re
import socket

import pytest

from sca.core.errors import SandboxError
from sca.core.models import Mount, SandboxSpec
from sca.sandbox.k8s import NETWORK, egress_manifests, pod_manifest, safe_name


def test_pod_security_resources_and_runtime():
    manifest = pod_manifest(
        "A_bad name",
        SandboxSpec(
            image="test",
            runtime="runsc",
            cpus=1.5,
            memory_mb=256,
            env={"FOO": "bar"},
            workdir="/work",
        ),
        run_as_uid=1001,
        run_as_gid=1002,
    )
    assert manifest["metadata"]["name"] == "sca-a-bad-name"
    pod = manifest["spec"]
    assert pod["runtimeClassName"] == "runsc"
    assert pod["securityContext"] == {"runAsUser": 1001, "runAsGroup": 1002}
    assert pod["restartPolicy"] == "Never"
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    container = pod["containers"][0]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["resources"]["limits"] == {"cpu": "1.5", "memory": "256Mi"}
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["command"] == ["sleep", "infinity"]
    assert container["env"] == [{"name": "FOO", "value": "bar"}]
    assert container["workingDir"] == "/work"


def test_hostpaths(tmp_path):
    file = tmp_path / "file"
    file.touch()
    spec = SandboxSpec(
        image="test",
        mounts=[
            Mount(source=str(tmp_path / "missing"), target="/work"),
            Mount(source=str(file), target="/file", read_only=True),
        ],
    )
    pod = pod_manifest("mounts", spec)["spec"]
    assert [v["hostPath"]["type"] for v in pod["volumes"]] == ["DirectoryOrCreate", "File"]
    assert pod["containers"][0]["volumeMounts"][1]["readOnly"] is True
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    "source,target", [("/tmp/docker.sock", "/socket"), ("/tmp/dir", "/docker.socket")]
)
def test_docker_socket_rejected(source, target):
    with pytest.raises(SandboxError):
        pod_manifest("bad", SandboxSpec(image="test", mounts=[Mount(source=source, target=target)]))


def test_host_socket_rejected(tmp_path):
    with socket.socket(socket.AF_UNIX) as sock:
        source = tmp_path / "sock"
        sock.bind(str(source))
        with pytest.raises(SandboxError):
            pod_manifest(
                "bad",
                SandboxSpec(image="test", mounts=[Mount(source=str(source), target="/socket")]),
            )


def test_names_and_labels():
    assert len(safe_name("A" * 100)) == 63
    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", safe_name("___Hello!!"))
    spec = SandboxSpec(
        image="test",
        labels={"BAD Prefix!/.bad key!": "!value!", "": "", "long": "a" * 100, NETWORK: "open"},
    )
    labels = pod_manifest("labels", spec)["metadata"]["labels"]
    assert labels["bad-prefix/bad-key"] == "value"
    assert labels["label"] == ""
    assert len(labels["long"]) == 63
    assert labels[NETWORK] == "restricted"


@pytest.mark.parametrize("network", ["open", "none", "restricted"])
def test_network_labels_and_proxy(network):
    manifest = pod_manifest(
        "test", SandboxSpec(image="test", network=network), proxy_url="http://10.96.0.2:3128"
    )
    assert manifest["metadata"]["labels"][NETWORK] == network
    env = {v["name"]: v["value"] for v in manifest["spec"]["containers"][0]["env"]}
    if network == "restricted":
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            assert env[key] == "http://10.96.0.2:3128"
        assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1"
    else:
        assert env == {}
    assert "runtimeClassName" not in manifest["spec"]


def test_egress_policies_and_restart():
    manifests = egress_manifests(["pypi.org"], namespace="test")
    assert all(m["metadata"]["namespace"] == "test" for m in manifests)
    policies = {m["metadata"]["name"]: m["spec"] for m in manifests if m["kind"] == "NetworkPolicy"}
    assert policies["sca-none"]["ingress"] == policies["sca-none"]["egress"] == []
    assert policies["sca-deny-egress"]["egress"] == []
    assert policies["sca-deny-egress"]["podSelector"]["matchExpressions"][0]["values"] == [
        "none",
        "restricted",
    ]
    restricted = policies["sca-restricted"]["egress"]
    assert restricted == [
        {
            "to": [{"podSelector": {"matchLabels": {"sca.role": "egress"}}}],
            "ports": [{"protocol": "TCP", "port": 3128}],
        }
    ]
    internet = policies["sca-proxy"]["egress"][0]["to"]
    assert "10.0.0.0/8" in internet[0]["ipBlock"]["except"]
    assert "fc00::/7" in internet[1]["ipBlock"]["except"]
    deployment = next(m for m in manifests if m["kind"] == "Deployment")
    pod = deployment["spec"]["template"]
    assert pod["spec"]["securityContext"]["runAsUser"] == 13
    assert pod["spec"]["containers"][0]["imagePullPolicy"] == "IfNotPresent"
    updated = next(m for m in egress_manifests([]) if m["kind"] == "Deployment")
    assert pod["metadata"]["annotations"] != updated["spec"]["template"]["metadata"]["annotations"]
    assert any(
        m["kind"] == "ConfigMap" and "pypi.org" in m["data"]["squid.conf"] for m in manifests
    )
    assert next(m for m in manifests if m["kind"] == "Service")["spec"]["ports"][0]["port"] == 3128


def test_calico_isolation_manifest():
    from sca.sandbox.k8s import isolation_manifests

    blocked = ["10.96.0.1/32", "172.18.0.0/16", "fd00::/64"]
    standard = isolation_manifests("test")
    manifests = isolation_manifests("test", network_policy_flavor="calico", blocked_cidrs=blocked)
    assert manifests[:2] == standard
    policy = manifests[2]
    assert policy["apiVersion"] == "crd.projectcalico.org/v1"
    assert policy["kind"] == "NetworkPolicy"
    assert policy["metadata"] == {"name": "sca-host-isolation", "namespace": "test"}
    spec = policy["spec"]
    assert spec["order"] == 0
    assert spec["selector"] == "sca.role == 'sandbox' && sca.network in {'none', 'restricted'}"
    assert spec["types"] == ["Egress"]
    assert spec["egress"] == [
        {"action": "Deny", "destination": {"nets": [*blocked[:2], "169.254.169.254/32"]}},
        {"action": "Deny", "destination": {"nets": [blocked[2]]}},
        {"action": "Deny", "protocol": "TCP", "destination": {"ports": [10250]}},
    ]


def test_blocked_cidrs_excluded_from_proxy():
    manifests = egress_manifests(
        [], network_policy_flavor="calico", blocked_cidrs=["203.0.113.0/24", "2001:db8::/32"]
    )
    policy = next(m for m in manifests if m["metadata"]["name"] == "sca-proxy")
    peers = policy["spec"]["egress"][0]["to"]
    assert "203.0.113.0/24" in peers[0]["ipBlock"]["except"]
    assert "2001:db8::/32" in peers[1]["ipBlock"]["except"]
    assert any(m["apiVersion"] == "crd.projectcalico.org/v1" for m in manifests)


@pytest.mark.asyncio
@pytest.mark.parametrize("network", ["none", "restricted"])
async def test_provider_applies_calico_to_both_modes(network):
    from unittest.mock import AsyncMock

    from sca.sandbox.k8s import K8sSandboxProvider

    provider = K8sSandboxProvider(network_policy_flavor="calico", blocked_cidrs=["172.18.0.0/16"])
    provider._cli = AsyncMock(
        side_effect=lambda *args, **kwargs: (
            (0, "10.96.0.1", "")
            if "kubernetes" in args
            else (0, '{"spec":{"clusterIP":"10.96.0.2"}}', "")
            if "service" in args
            else (0, "", "")
        )
    )
    provider._apply = AsyncMock()
    await provider.create("test", SandboxSpec(image="test", network=network))
    manifests = [m for call in provider._apply.call_args_list for m in call.args[0]]
    policy = next(m for m in manifests if m["metadata"]["name"] == "sca-host-isolation")
    assert policy["spec"]["egress"][0]["destination"]["nets"] == [
        "172.18.0.0/16",
        "10.96.0.1/32",
        "169.254.169.254/32",
    ]


def test_network_policy_settings():
    from sca.config import Settings

    settings = Settings(_env_file=None)
    assert settings.k8s_network_policy_flavor == "standard"
    assert settings.k8s_blocked_cidrs == []
    with pytest.raises(ValueError):
        Settings(_env_file=None, k8s_network_policy_flavor="unknown")


def test_sync_mode_uses_valid_empty_dirs_and_no_host_paths():
    import re

    spec = SandboxSpec(image="img", labels={"sca.state_transport": "sync"})
    pod = pod_manifest("sync", spec, namespace="ns")["spec"]
    names = {v["name"]: v for v in pod["volumes"]}
    assert all(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name) for name in names)
    assert all("hostPath" not in v for v in pod["volumes"])
    targets = {m["mountPath"]: m["name"] for m in pod["containers"][0]["volumeMounts"]}
    assert "emptyDir" in names[targets["/workspace"]]
    assert "emptyDir" in names[targets["/agent_home"]]


def test_pool_hash_template_and_env_allowlist():
    from sca.sandbox.k8s import pool_compatible, pool_hash

    spec = SandboxSpec(image="img", labels={"sca.state_transport": "sync"})
    assert pool_hash(spec) == pool_hash(spec.model_copy(update={"env": {"SCA_TEST": "x"}}))
    for field, value in {
        "image": "other",
        "cpus": 2,
        "memory_mb": 256,
        "network": "none",
        "runtime": "runsc",
    }.items():
        assert pool_hash(spec) != pool_hash(spec.model_copy(update={field: value}))
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "CODEX_HOME", "PIP_INDEX_URL", "SCA_TEST"):
        assert pool_compatible(spec.model_copy(update={"env": {key: "setting"}}))
    for key in ("SCA_MODEL_KEY", "TOKEN", "http_proxy", "AWS_SECRET_ACCESS_KEY"):
        assert not pool_compatible(spec.model_copy(update={"env": {key: "secret"}}))
    assert not pool_compatible(spec.model_copy(update={"labels": {}}))
    assert not pool_compatible(
        spec.model_copy(update={"mounts": [Mount(source="/tmp", target="/work")]})
    )


async def test_pool_claim_conflict_and_annotation():
    import json
    from unittest.mock import AsyncMock

    from sca.sandbox.k8s import K8sSandboxProvider

    provider = K8sSandboxProvider(pool_size=2)
    pods = [
        {"metadata": {"name": name, "resourceVersion": str(i)}, "status": {"phase": "Running"}}
        for i, name in enumerate(("one", "two"))
    ]
    provider._pods = AsyncMock(return_value=pods)
    provider._cli = AsyncMock(side_effect=[(1, "", "Conflict"), (0, "", "")])
    spec = SandboxSpec(
        image="img",
        labels={"sca.state_transport": "sync", "sca.task_id": "task"},
        env={"SCA_TEST": "value"},
    )
    assert await provider._claim("task-name", spec) == "two"
    patch = json.loads(provider._cli.call_args.args[-1])["metadata"]
    assert patch["resourceVersion"] == "1"
    assert patch["labels"]["sca.state"] == "claimed"
    assert patch["labels"]["sca.name"] == "sca-task-name"
    assert json.loads(patch["annotations"]["sca.pool-env"]) == spec.env


@pytest.mark.parametrize(
    "changes",
    [
        {"labels": {}},
        {"env": {"TOKEN": "secret"}},
        {"mounts": [Mount(source="/tmp", target="/work")]},
    ],
)
async def test_incompatible_create_bypasses_pool(changes):
    from unittest.mock import AsyncMock

    from sca.sandbox.k8s import K8sSandboxProvider

    provider = K8sSandboxProvider(pool_size=2)
    provider._pods = AsyncMock(return_value=[])
    provider._cli = AsyncMock(return_value=(0, "", ""))
    provider._apply = AsyncMock()
    provider._claim = AsyncMock()
    spec = SandboxSpec(image="img", network="open", labels={"sca.state_transport": "sync"})
    assert await provider.create("normal", spec.model_copy(update=changes)) == "sca-normal"
    provider._claim.assert_not_awaited()


async def test_pool_replenish_has_no_task_env():
    from unittest.mock import AsyncMock

    from sca.sandbox.k8s import K8sSandboxProvider, pool_hash

    provider = K8sSandboxProvider(pool_size=2)
    provider._namespace = AsyncMock()
    provider._pods = AsyncMock(return_value=[])
    provider.create = AsyncMock()
    spec = SandboxSpec(image="img", env={"TOKEN": "secret"}, labels={"sca.task_id": "task"})
    await provider.replenish(spec, 2)
    assert provider.create.await_count == 2
    for call in provider.create.call_args_list:
        template = call.args[1]
        assert template.env == {}
        assert template.mounts == []
        assert template.labels == {
            "sca.pool": pool_hash(spec),
            "sca.state": "idle",
            "sca.state_transport": "sync",
        }


async def test_pool_exec_recovers_defaults_and_explicit_env_wins(monkeypatch):
    import json
    from unittest.mock import AsyncMock

    from sca.core.models import ExecExit
    from sca.sandbox.k8s import K8sSandboxProvider

    provider = K8sSandboxProvider()
    provider._cli = AsyncMock(
        return_value=(
            0,
            json.dumps(
                {
                    "metadata": {
                        "annotations": {
                            "sca.pool-env": json.dumps(
                                {"SCA_TEST": "default", "CODEX_HOME": "/agent_home"}
                            ),
                            "sca.workdir": "/agent_home",
                        }
                    }
                }
            ),
            "",
        )
    )
    commands = []

    async def stream(command, cleanup, **kwargs):
        commands.append(command("os.execvp(argv[0], argv)", "marker"))
        yield ExecExit(exit_code=0)

    monkeypatch.setattr("sca.sandbox.k8s.stream_exec", stream)
    assert [
        event async for event in provider.exec("claimed", ["true"], env={"SCA_TEST": "override"})
    ] == [ExecExit(exit_code=0)]
    wrapper = commands[0][commands[0].index("-c") + 1]
    assert "override" in wrapper and "default" not in wrapper
    assert "CODEX_HOME" in wrapper
    assert "os.chdir('/agent_home')" in wrapper


async def test_pool_name_recovery_even_when_disabled():
    from unittest.mock import AsyncMock

    from sca.sandbox.k8s import K8sSandboxProvider

    provider = K8sSandboxProvider(pool_size=0)
    provider._pods = AsyncMock(
        return_value=[{"metadata": {"name": "sca-pool-original"}, "status": {"phase": "Running"}}]
    )
    provider._cli = AsyncMock()
    assert await provider.create("same-name", SandboxSpec(image="img")) == "sca-pool-original"
    provider._cli.assert_not_awaited()


async def test_replenish_obsolete_delete_has_claim_precondition():
    import json
    from unittest.mock import AsyncMock

    from sca.sandbox.k8s import K8sSandboxProvider

    provider = K8sSandboxProvider()
    provider._namespace = AsyncMock()
    provider._pods = AsyncMock(
        return_value=[
            {
                "metadata": {
                    "name": "old",
                    "uid": "unique",
                    "resourceVersion": "42",
                    "labels": {"sca.pool": "obsolete"},
                },
                "status": {"phase": "Running"},
            }
        ]
    )
    provider._cli = AsyncMock(return_value=(1, "", "Conflict"))
    await provider.replenish(SandboxSpec(image="img"), 0)
    options = json.loads(provider._cli.call_args.kwargs["data"])
    assert options["preconditions"] == {"uid": "unique", "resourceVersion": "42"}
