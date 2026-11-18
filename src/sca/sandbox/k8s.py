from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import stat
from collections.abc import AsyncIterator
from ipaddress import ip_network
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

from sca.core.errors import SandboxError
from sca.core.models import ExecEvent, SandboxSpec
from sca.sandbox._exec import stream_exec
from sca.sandbox._transfer import transfer
from sca.sandbox.egress import CA_ENV, CA_TARGET, SQUID_IMAGE, prepare_ca, render_squid_conf

UID, GID = os.getuid(), os.getgid()

NETWORK = "sca.network"
PROXY = {"sca.role": "egress"}
CA_LABEL = "sca.ca-fingerprint"


def pool_hash(spec: SandboxSpec) -> str:
    template = [spec.image, spec.cpus, spec.memory_mb, spec.network, spec.runtime]
    return hashlib.sha256(json.dumps(template).encode()).hexdigest()[:32]


def pool_env_allowed(env: dict[str, str]) -> bool:
    return all(
        key != "SCA_MODEL_KEY"
        and (
            key in {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "CODEX_HOME"}
            or key.startswith(("PIP_", "SCA_"))
        )
        for key in env
    )


def pool_compatible(spec: SandboxSpec) -> bool:
    return (
        spec.labels.get("sca.state_transport") == "sync"
        and not spec.mounts
        and pool_env_allowed(spec.env)
    )


def safe_name(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", f"sca-{name}".lower())[:63].strip("-")


def _label(value: str, *, empty: bool = True) -> str:
    value = re.sub(r"[^a-zA-Z0-9_.-]", "-", value)[:63].strip("-_.")
    return value or ("" if empty else "label")


def _labels(labels: dict[str, str]) -> dict[str, str]:
    result = {}
    for key, value in labels.items():
        if "/" in key:
            prefix, key = key.rsplit("/", 1)
            prefix = ".".join(
                re.sub(r"[^a-z0-9-]", "-", part.lower())[:63].strip("-") or "label"
                for part in prefix.split(".")
            )[:253].strip("-.")
            key = prefix + "/" + _label(key, empty=False)
        else:
            key = _label(key, empty=False)
        result[key] = _label(value)
    return result


def pod_manifest(
    name: str,
    spec: SandboxSpec,
    *,
    namespace: str = "sca-sandboxes",
    run_as_uid: int = UID,
    run_as_gid: int = GID,
    proxy_url: str | None = None,
    node_selector: dict[str, str] | None = None,
    ca_fingerprint: str = "",
) -> dict:
    env = dict(spec.env)
    if spec.network == "restricted" and proxy_url:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.setdefault(key, proxy_url)
        for key in ("NO_PROXY", "no_proxy"):
            env.setdefault(key, "localhost,127.0.0.1")
    volumes, mounts = [], []
    if spec.network == "restricted":
        env.update(dict.fromkeys(CA_ENV, CA_TARGET))
        volumes.append(
            {
                "name": "egress-ca",
                "configMap": {
                    "name": f"sca-egress-bundle-{ca_fingerprint}"
                    if ca_fingerprint
                    else "sca-egress-bundle",
                    "items": [{"key": "ca-bundle.crt", "path": "ca-bundle.crt"}],
                },
            }
        )
        mounts.append(
            {
                "name": "egress-ca",
                "mountPath": CA_TARGET,
                "subPath": "ca-bundle.crt",
                "readOnly": True,
            }
        )
    for index, mount in enumerate(spec.mounts):
        source = Path(mount.source).resolve()
        if (
            source.name in {"docker.sock", "docker.socket"}
            or mount.target.endswith(("/docker.sock", "/docker.socket"))
            or (source.exists() and stat.S_ISSOCK(source.stat().st_mode))
        ):
            raise SandboxError("Mounting host sockets is forbidden")
        volumes.append(
            {
                "name": f"mount-{index}",
                "hostPath": {
                    "path": str(source),
                    "type": "File" if source.is_file() else "DirectoryOrCreate",
                },
            }
        )
        mounts.append(
            {"name": f"mount-{index}", "mountPath": mount.target, "readOnly": mount.read_only}
        )
    sync = spec.labels.get("sca.state_transport") == "sync"
    for target in ("/workspace", "/agent_home") if sync else ():
        if not any(m["mountPath"] == target for m in mounts):
            name_ = target.removeprefix("/").replace("_", "-")  # volume names are DNS labels
            volumes.append({"name": name_, "emptyDir": {}})
            mounts.append({"name": name_, "mountPath": target})
    pod = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": {
            "runAsUser": run_as_uid,
            "runAsGroup": run_as_gid,
            **({"fsGroup": run_as_gid} if sync else {}),
        },
        "containers": [
            {
                "name": "sandbox",
                "image": spec.image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sleep", "infinity"],
                "workingDir": spec.workdir,
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                },
                "resources": {"limits": {"cpu": str(spec.cpus), "memory": f"{spec.memory_mb}Mi"}},
                "env": [{"name": k, "value": v} for k, v in env.items()],
                "volumeMounts": mounts,
            }
        ],
        "volumes": volumes,
    }
    if sync:
        pod["initContainers"] = [
            {
                "name": "own-state",
                "image": spec.image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["chown", f"{run_as_uid}:{run_as_gid}", "/workspace", "/agent_home"],
                "volumeMounts": [
                    m for m in mounts if m["mountPath"] in {"/workspace", "/agent_home"}
                ],
                "securityContext": {
                    "runAsUser": 0,
                    "runAsGroup": 0,
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"], "add": ["CHOWN"]},
                },
            }
        ]
    if node_selector:
        pod["nodeSelector"] = node_selector
    if spec.runtime:
        pod["runtimeClassName"] = spec.runtime
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": safe_name(name),
            "namespace": namespace,
            "labels": {
                **_labels(spec.labels),
                NETWORK: spec.network,
                "sca.role": "sandbox",
                **({CA_LABEL: ca_fingerprint} if ca_fingerprint else {}),
            },
        },
        "spec": pod,
    }


def _policy(name: str, namespace: str, selector: dict, **spec) -> dict:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"podSelector": selector, **spec},
    }


def isolation_manifests(
    namespace: str,
    *,
    network_policy_flavor: Literal["standard", "calico"] = "standard",
    blocked_cidrs: list[str] | None = None,
) -> list[dict]:
    blocked = list(dict.fromkeys(str(ip_network(c)) for c in blocked_cidrs or []))
    manifests = [
        _policy(
            "sca-deny-egress",
            namespace,
            {
                "matchExpressions": [
                    {"key": NETWORK, "operator": "In", "values": ["none", "restricted"]}
                ]
            },
            policyTypes=["Egress"],
            egress=[],
        ),
        _policy(
            "sca-none",
            namespace,
            {"matchLabels": {NETWORK: "none"}},
            policyTypes=["Ingress", "Egress"],
            ingress=[],
            egress=[],
        ),
    ]

    if network_policy_flavor == "calico":
        manifests.append(
            {
                "apiVersion": "crd.projectcalico.org/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "sca-host-isolation", "namespace": namespace},
                "spec": {
                    "order": 0,
                    "selector": "sca.role == 'sandbox' && sca.network in {'none', 'restricted'}",
                    "types": ["Egress"],
                    "egress": [
                        *[
                            {"action": "Deny", "destination": {"nets": nets}}
                            for version in (4, 6)
                            if (
                                nets := [
                                    c
                                    for c in dict.fromkeys([*blocked, "169.254.169.254/32"])
                                    if ip_network(c).version == version
                                ]
                            )
                        ],
                        {"action": "Deny", "protocol": "TCP", "destination": {"ports": [10250]}},
                    ],
                },
            }
        )
    return manifests


def egress_manifests(
    allowlist: list[str],
    *,
    namespace: str = "sca-sandboxes",
    image: str = SQUID_IMAGE,
    network_policy_flavor: Literal["standard", "calico"] = "standard",
    blocked_cidrs: list[str] | None = None,
    ca_fingerprint: str = "",
) -> list[dict]:
    conf = render_squid_conf(allowlist)
    metadata = {"name": "sca-egress", "namespace": namespace}
    private = [
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
    ]
    excluded = [ip_network(c) for c in blocked_cidrs or []]
    return [
        *isolation_manifests(
            namespace, network_policy_flavor=network_policy_flavor, blocked_cidrs=blocked_cidrs
        ),
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata,
            "data": {"squid.conf": conf},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": metadata,
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": PROXY},
                "template": {
                    "metadata": {
                        "labels": {
                            **PROXY,
                            **({CA_LABEL: ca_fingerprint} if ca_fingerprint else {}),
                        },
                        "annotations": {
                            "sca.config-hash": hashlib.sha256(conf.encode()).hexdigest()
                        },
                    },
                    "spec": {
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "securityContext": {"runAsUser": 13, "runAsGroup": 13, "fsGroup": 13},
                        "volumes": [
                            {
                                "name": "egress-ca",
                                "secret": {
                                    "secretName": f"sca-egress-ca-{ca_fingerprint}"
                                    if ca_fingerprint
                                    else "sca-egress-ca",
                                    "defaultMode": 0o440,
                                },
                            }
                        ],
                        "containers": [
                            {
                                "name": "squid",
                                "volumeMounts": [
                                    {"name": "egress-ca", "mountPath": "/ca", "readOnly": True}
                                ],
                                "image": image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "sh",
                                    "-c",
                                    (
                                        "set -eu; umask 077; printf '%s' \"$SQUID_CONF\" > /tmp/squid.conf; "
                                        "/usr/lib/squid/security_file_certgen -c -s /tmp/ssl_db -M 4MB; "
                                        "exec squid -N -f /tmp/squid.conf"
                                    ),
                                ],
                                "env": [
                                    {
                                        "name": "SQUID_CONF",
                                        "valueFrom": {
                                            "configMapKeyRef": {
                                                "name": "sca-egress",
                                                "key": "squid.conf",
                                            }
                                        },
                                    }
                                ],
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "limits": {"cpu": "250m", "memory": "256Mi"},
                                    "requests": {"cpu": "10m", "memory": "32Mi"},
                                },
                                "readinessProbe": {"tcpSocket": {"port": 3128}},
                            }
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": metadata,
            "spec": {"selector": PROXY, "ports": [{"port": 3128, "targetPort": 3128}]},
        },
        _policy(
            "sca-restricted",
            namespace,
            {"matchLabels": {NETWORK: "restricted"}},
            policyTypes=["Egress"],
            egress=[
                {
                    "to": [{"podSelector": {"matchLabels": PROXY}}],
                    "ports": [{"protocol": "TCP", "port": 3128}],
                }
            ],
        ),
        _policy(
            "sca-proxy",
            namespace,
            {"matchLabels": PROXY},
            policyTypes=["Egress"],
            egress=[
                {
                    "to": [
                        {
                            "ipBlock": {
                                "cidr": "0.0.0.0/0",
                                "except": list(
                                    dict.fromkeys(
                                        [*private, *[str(n) for n in excluded if n.version == 4]]
                                    )
                                ),
                            }
                        },
                        {
                            "ipBlock": {
                                "cidr": "::/0",
                                "except": list(
                                    dict.fromkeys(
                                        [
                                            "::/128",
                                            "::1/128",
                                            "fc00::/7",
                                            "fe80::/10",
                                            *[str(n) for n in excluded if n.version == 6],
                                        ]
                                    )
                                ),
                            }
                        },
                    ]
                },
                # Cluster DNS is the only private destination the proxy needs.
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                            },
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                        }
                    ],
                    "ports": [{"protocol": p, "port": 53} for p in ("UDP", "TCP")],
                },
            ],
        ),
    ]


class K8sSandboxProvider:
    """kubectl-backed sandboxes. Phase-2 hostPath mounts require the worker and
    node to share a filesystem at the same paths.
    """

    def __init__(
        self,
        kubectl_bin="kubectl",
        *,
        context=None,
        namespace="sca-sandboxes",
        egress_allowlist=None,
        egress_image=SQUID_IMAGE,
        run_as_uid=UID,
        run_as_gid=GID,
        node_selector: dict[str, str] | None = None,
        network_policy_flavor: Literal["standard", "calico"] = "standard",
        blocked_cidrs: list[str] | None = None,
        pool_size: int = 0,
        egress_ca_dir: Path | None = None,
    ):
        self.kubectl_bin, self.context, self.namespace = kubectl_bin, context, namespace
        self.egress_allowlist, self.egress_image = egress_allowlist or [], egress_image
        self.run_as_uid, self.run_as_gid = run_as_uid, run_as_gid
        self.node_selector = node_selector
        self.network_policy_flavor = network_policy_flavor
        self.blocked_cidrs = [str(ip_network(c)) for c in blocked_cidrs or []]
        self._egress_lock = asyncio.Lock()
        self.pool_size = pool_size
        from sca.config import get_settings

        settings = get_settings()
        self.egress_ca_dir = (
            egress_ca_dir
            or settings.egress_ca_dir
            or settings.data_dir / "egress" / safe_name(namespace)
        ).resolve()
        self._ca_fingerprint = ""

    def _command(self, *args):
        return [
            self.kubectl_bin,
            *(["--context", self.context] if self.context else []),
            "--namespace",
            self.namespace,
            "--request-timeout=190s",
            *args,
        ]

    async def _cli(self, *args, check=True, data=None):
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command(*args),
                stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate(data)
        except OSError as exc:
            raise SandboxError(str(exc)) from exc
        out, err = out.decode(errors="replace"), err.decode(errors="replace")
        if check and proc.returncode:
            raise SandboxError(err)
        return proc.returncode, out, err

    async def _apply(self, manifests):
        await self._cli(
            "apply",
            "-f",
            "-",
            data=json.dumps({"apiVersion": "v1", "kind": "List", "items": manifests}).encode(),
        )

    async def _namespace(self):
        await self._apply(
            [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": self.namespace}}]
        )
        # Pods cannot be created until the namespace's default service account exists, and
        # the controller that creates it runs asynchronously.
        await self._cli("wait", "--for=create", "serviceaccount/default", "--timeout=60s")

    async def _isolation_settings(self) -> dict:
        blocked = list(self.blocked_cidrs)
        if self.network_policy_flavor == "calico":
            _, address, _ = await self._cli(
                "get",
                "service",
                "kubernetes",
                "--namespace",
                "default",
                "-o",
                "jsonpath={.spec.clusterIP}",
            )
            blocked.append(str(ip_network(address.strip())))
        return {"network_policy_flavor": self.network_policy_flavor, "blocked_cidrs": blocked}

    async def ensure_egress(self):
        async with self._egress_lock:
            await self._namespace()
            await prepare_ca(self.egress_ca_dir)
            cert = (self.egress_ca_dir / "ca.crt").read_text()
            bundle = (self.egress_ca_dir / "ca-bundle.crt").read_text()
            self._ca_fingerprint = hashlib.sha256((cert + bundle).encode()).hexdigest()[:32]
            fingerprint = self._ca_fingerprint
            # Create avoids kubectl's last-applied annotation retaining private material.
            secret = {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "Opaque",
                "metadata": {
                    "name": f"sca-egress-ca-{fingerprint}",
                    "namespace": self.namespace,
                    "labels": {CA_LABEL: fingerprint},
                },
                "data": {
                    name: base64.b64encode((self.egress_ca_dir / name).read_bytes()).decode()
                    for name in ("ca.crt", "ca.key")
                },
            }
            code, _, _ = await self._cli(
                "create", "-f", "-", check=False, data=json.dumps(secret).encode()
            )
            if code:
                # Replace also repairs a pre-existing resource without logging key data.
                code, _, _ = await self._cli(
                    "replace", "-f", "-", check=False, data=json.dumps(secret).encode()
                )
                if code:
                    raise SandboxError("Unable to publish egress CA Secret")
            await self._apply(
                [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {
                            "name": f"sca-egress-bundle-{fingerprint}",
                            "namespace": self.namespace,
                            "labels": {CA_LABEL: fingerprint},
                        },
                        "data": {"ca-bundle.crt": bundle},
                    }
                ]
            )
            await self._apply(
                egress_manifests(
                    self.egress_allowlist,
                    namespace=self.namespace,
                    image=self.egress_image,
                    ca_fingerprint=self._ca_fingerprint,
                    **await self._isolation_settings(),
                )
            )
            await self._cli("rollout", "status", "deployment/sca-egress", "--timeout=180s")
            _, out, _ = await self._cli("get", "service", "sca-egress", "-o", "json")
            return f"http://{json.loads(out)['spec']['clusterIP']}:3128"

    async def _pods(self, selector: str) -> list[dict]:
        _, raw, _ = await self._cli("get", "pods", "-l", selector, "-o", "json")
        return json.loads(raw).get("items", []) if raw.strip() else []

    async def task_id(self, sandbox_id: str) -> str | None:
        _, raw, _ = await self._cli("get", "pod", sandbox_id, "-o", "json")
        return json.loads(raw)["metadata"].get("labels", {}).get("sca.task_id")

    async def _claim(self, name: str, spec: SandboxSpec) -> str | None:
        selector = f"sca.pool={pool_hash(spec)},sca.state=idle"
        if spec.network == "restricted":
            selector += f",{CA_LABEL}={self._ca_fingerprint}"
        for pod in await self._pods(selector):
            if pod.get("status", {}).get("phase") != "Running" or pod["metadata"].get(
                "deletionTimestamp"
            ):
                continue
            metadata = pod["metadata"]
            patch = {
                "metadata": {
                    "resourceVersion": metadata["resourceVersion"],
                    "labels": {
                        **_labels(spec.labels),
                        "sca.name": safe_name(name),
                        "sca.state": "claimed",
                    },
                    "annotations": {
                        "sca.pool-env": json.dumps(spec.env),
                        "sca.workdir": spec.workdir,
                    },
                }
            }
            code, _, err = await self._cli(
                "patch",
                "pod",
                metadata["name"],
                "--type=merge",
                "-p",
                json.dumps(patch),
                check=False,
            )
            if not code:
                return metadata["name"]
            if "Conflict" not in err and "NotFound" not in err:
                raise SandboxError("Unable to claim pool pod")
        return None

    async def replenish(self, spec_template: SandboxSpec, size: int) -> None:
        await self._namespace()
        if spec_template.network == "restricted":
            await self.ensure_egress()
        template_hash = pool_hash(spec_template)
        idle = await self._pods("sca.pool,sca.state=idle")
        current = []
        for pod in idle:
            metadata = pod["metadata"]
            if (
                size <= 0
                or (
                    spec_template.network == "restricted"
                    and metadata["labels"].get(CA_LABEL) != self._ca_fingerprint
                )
                or metadata["labels"]["sca.pool"] != template_hash
                or pod.get("status", {}).get("phase") in {"Failed", "Succeeded"}
            ):
                # A concurrent claim must prevent deletion of a now-used sandbox.
                options = {
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "preconditions": {
                        "uid": metadata["uid"],
                        "resourceVersion": metadata["resourceVersion"],
                    },
                }
                code, _, err = await self._cli(
                    "delete",
                    "--raw",
                    f"/api/v1/namespaces/{self.namespace}/pods/{metadata['name']}",
                    "-f",
                    "-",
                    check=False,
                    data=json.dumps(options).encode(),
                )
                if code and "Conflict" not in err and "NotFound" not in err:
                    raise SandboxError("Unable to remove obsolete pool pod")
            elif not metadata.get("deletionTimestamp"):
                current.append(pod)
        template = spec_template.model_copy(
            update={
                "env": {},
                "mounts": [],
                "workdir": "/workspace",
                "labels": {
                    "sca.state_transport": "sync",
                    "sca.pool": template_hash,
                    "sca.state": "idle",
                },
            }
        )
        for _ in range(max(0, size - len(current))):
            await self.create(f"pool-{uuid4().hex}", template)

    async def create(self, name: str, spec: SandboxSpec) -> str:
        sid = safe_name(name)
        if spec.labels.get("sca.state") != "idle":
            existing = await self._pods(f"sca.name={sid},sca.state=claimed")
            for pod in existing:
                if pod.get("status", {}).get("phase") == "Running":
                    return pod["metadata"]["name"]
        _, out, _ = await self._cli("get", "pod", sid, "--ignore-not-found", "-o", "json")
        if out.strip():
            if json.loads(out).get("status", {}).get("phase") == "Running":
                return sid
            await self.destroy(sid)
            await self._cli("wait", "--for=delete", f"pod/{sid}", "--timeout=180s")
        if spec.network == "restricted" and self.pool_size > 0:
            await self.ensure_egress()
        if self.pool_size > 0 and spec.labels.get("sca.state") != "idle" and pool_compatible(spec):
            claimed = await self._claim(name, spec)
            if claimed:
                return claimed
        await self._namespace()
        proxy = None
        if spec.network == "restricted":
            proxy = await self.ensure_egress()
        elif spec.network == "none":
            await self._apply(
                isolation_manifests(self.namespace, **await self._isolation_settings())
            )
        # Create missing directories as the worker, not as the node's root kubelet.
        for mount in spec.mounts:
            source = Path(mount.source).resolve()
            if not source.exists():
                source.mkdir(parents=True, exist_ok=True)
        await self._apply(
            [
                pod_manifest(
                    name,
                    spec,
                    namespace=self.namespace,
                    run_as_uid=self.run_as_uid,
                    run_as_gid=self.run_as_gid,
                    proxy_url=proxy,
                    node_selector=self.node_selector,
                    ca_fingerprint=self._ca_fingerprint if proxy else "",
                )
            ]
        )
        code, _, err = await self._cli(
            "wait", "--for=condition=Ready", f"pod/{sid}", "--timeout=180s", check=False
        )
        if code:
            _, status, _ = await self._cli("get", "pod", sid, "-o", "json", check=False)
            _, events, _ = await self._cli(
                "get", "events", "--field-selector", f"involvedObject.name={sid}", check=False
            )
            raise SandboxError(f"{err}\n{status}\n{events}")
        return sid

    async def list(self, labels: dict[str, str]) -> list[str]:
        selector = ",".join(key + ("=" + value if value else "") for key, value in labels.items())
        _, out, _ = await self._cli(
            "get", "pods", *(["-l", selector] if selector else []), "-o", "json"
        )
        return [item["metadata"]["name"] for item in json.loads(out)["items"]]

    async def exists(self, sandbox_id):
        _, out, _ = await self._cli("get", "pod", sandbox_id, "--ignore-not-found", "-o", "name")
        return bool(out.strip())

    async def destroy(self, sandbox_id):
        await self._cli(
            "delete",
            "pod",
            sandbox_id,
            "--ignore-not-found",
            "--grace-period=0",
            "--force",
            "--wait=false",
        )

    async def exec(
        self,
        sandbox_id: str,
        argv: list[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_s: float | None = None,
    ) -> AsyncIterator[ExecEvent]:
        _, raw, _ = await self._cli("get", "pod", sandbox_id, "-o", "json")
        annotations = json.loads(raw)["metadata"].get("annotations", {})
        defaults = json.loads(annotations.get("sca.pool-env", "{}"))
        env = {**defaults, **(env or {})}
        workdir = workdir or annotations.get("sca.workdir")

        def command(wrapper, marker):
            setup = (
                "import json\n"
                + f"os.environ.update(json.loads({json.dumps(env or {})!r}))\n"
                + (f"os.chdir({workdir!r})\n" if workdir else "")
            )
            wrapper = wrapper.replace("os.execvp(", setup + "os.execvp(")
            return self._command(
                "exec", "-i", sandbox_id, "--", "python3", "-c", wrapper, marker, *argv
            )

        async def cleanup(script, marker):
            await self._cli("exec", sandbox_id, "--", "python3", "-c", script, marker, check=False)

        async for event in stream_exec(command, cleanup, stdin=stdin, timeout_s=timeout_s):
            yield event

    async def upload(self, sandbox_id: str, target_dir: str, archive: AsyncIterator[bytes]) -> None:
        await self._cli("exec", sandbox_id, "--", "mkdir", "-p", "--", target_dir)
        async for _ in transfer(
            self._command(
                "exec",
                "-i",
                sandbox_id,
                "--",
                "tar",
                "--no-same-owner",
                "-xf",
                "-",
                "-C",
                target_dir,
            ),
            archive,
        ):
            pass

    def download(self, sandbox_id: str, path: str) -> AsyncIterator[bytes]:
        item = PurePosixPath(path)
        return transfer(
            self._command(
                "exec",
                sandbox_id,
                "--",
                "tar",
                "-cf",
                "-",
                "-C",
                str(item.parent),
                "--",
                item.name,
            )
        )
