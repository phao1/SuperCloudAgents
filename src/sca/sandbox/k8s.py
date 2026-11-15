from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from sca.core.errors import SandboxError
from sca.core.models import SandboxSpec
from sca.sandbox._exec import stream_exec
from sca.sandbox.egress import SQUID_IMAGE, render_squid_conf

UID, GID = os.getuid(), os.getgid()

NETWORK = "sca.network"
PROXY = {"sca.role": "egress"}


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
) -> dict:
    env = dict(spec.env)
    if spec.network == "restricted" and proxy_url:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.setdefault(key, proxy_url)
        for key in ("NO_PROXY", "no_proxy"):
            env.setdefault(key, "localhost,127.0.0.1")
    volumes, mounts = [], []
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
    pod = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "securityContext": {"runAsUser": run_as_uid, "runAsGroup": run_as_gid},
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
    if spec.runtime:
        pod["runtimeClassName"] = spec.runtime
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": safe_name(name),
            "namespace": namespace,
            "labels": {**_labels(spec.labels), NETWORK: spec.network, "sca.role": "sandbox"},
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


def isolation_manifests(namespace: str) -> list[dict]:
    return [
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


def egress_manifests(
    allowlist: list[str], *, namespace: str = "sca-sandboxes", image: str = SQUID_IMAGE
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
    return [
        *isolation_manifests(namespace),
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
                        "labels": PROXY,
                        "annotations": {
                            "sca.config-hash": hashlib.sha256(conf.encode()).hexdigest()
                        },
                    },
                    "spec": {
                        "automountServiceAccountToken": False,
                        "enableServiceLinks": False,
                        "securityContext": {"runAsUser": 13, "runAsGroup": 13},
                        "containers": [
                            {
                                "name": "squid",
                                "image": image,
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "sh",
                                    "-c",
                                    (
                                        'printf "%s" "$SQUID_CONF" > /tmp/squid.conf && '
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
                        {"ipBlock": {"cidr": "0.0.0.0/0", "except": private}},
                        {
                            "ipBlock": {
                                "cidr": "::/0",
                                "except": ["::/128", "::1/128", "fc00::/7", "fe80::/10"],
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
    ):
        self.kubectl_bin, self.context, self.namespace = kubectl_bin, context, namespace
        self.egress_allowlist, self.egress_image = egress_allowlist or [], egress_image
        self.run_as_uid, self.run_as_gid = run_as_uid, run_as_gid
        self._egress_lock = asyncio.Lock()

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

    async def ensure_egress(self):
        async with self._egress_lock:
            await self._namespace()
            await self._apply(
                egress_manifests(
                    self.egress_allowlist, namespace=self.namespace, image=self.egress_image
                )
            )
            await self._cli("rollout", "status", "deployment/sca-egress", "--timeout=180s")
            _, out, _ = await self._cli("get", "service", "sca-egress", "-o", "json")
            return f"http://{json.loads(out)['spec']['clusterIP']}:3128"

    async def create(self, name: str, spec: SandboxSpec) -> str:
        sid = safe_name(name)
        _, out, _ = await self._cli("get", "pod", sid, "--ignore-not-found", "-o", "json")
        if out.strip():
            if json.loads(out).get("status", {}).get("phase") == "Running":
                return sid
            await self.destroy(sid)
            await self._cli("wait", "--for=delete", f"pod/{sid}", "--timeout=180s")
        await self._namespace()
        proxy = None
        if spec.network == "restricted":
            proxy = await self.ensure_egress()
        elif spec.network == "none":
            await self._apply(isolation_manifests(self.namespace))
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

    def exec(self, sandbox_id, argv, *, workdir=None, env=None, stdin=None, timeout_s=None):
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

        return stream_exec(command, cleanup, stdin=stdin, timeout_s=timeout_s)
