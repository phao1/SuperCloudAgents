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
