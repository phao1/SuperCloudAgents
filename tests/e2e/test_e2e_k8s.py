"""B1 on a real multi-node cluster: the whole platform, sandboxes on another node, no host mounts.

Needs the kind cluster from deploy/kind/cluster-2node-calico.yaml (context kind-sca) with the
sandbox image loaded. The worker runs on this machine; sandboxes run on the worker node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from .test_e2e_fake import ADD_SUB, RIGHT_FIX, block

pytestmark = [pytest.mark.e2e, pytest.mark.k8s]

KUBECTL = shutil.which("kubectl") or str(Path.home() / ".local/bin/kubectl")
NAMESPACE = "sca-e2e"


def kubectl(*args: str) -> str:
    return subprocess.run(
        [KUBECTL, "--context", "kind-sca", *args], check=True, capture_output=True, text=True
    ).stdout


def _worker_node() -> str | None:
    try:
        nodes = json.loads(kubectl("get", "nodes", "-o", "json", "--request-timeout=5s"))["items"]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    workers = [
        n["metadata"]["name"]
        for n in nodes
        if "node-role.kubernetes.io/control-plane" not in n["metadata"]["labels"]
    ]
    return workers[0] if workers else None


@pytest.fixture(scope="module")
def worker_node() -> str:
    node = _worker_node()
    if node is None:
        pytest.skip("needs the two-node kind cluster (deploy/kind/cluster-2node-calico.yaml)")
    return node


@pytest.fixture
def state_transport() -> str:
    return "sync"


@pytest.fixture
def settings_overrides(worker_node) -> dict:
    return {
        "sandbox_driver": "k8s",
        "kubectl_bin": KUBECTL,
        "k8s_context": "kind-sca",
        "k8s_namespace": NAMESPACE,
        "k8s_node_selector": {"kubernetes.io/hostname": worker_node},
        "sandbox_cpus": 0.5,
        "sandbox_memory_mb": 512,
    }


async def test_full_task_on_another_node_without_host_mounts(stack, source_repo, worker_node):
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": f"Fix add.\n{block(RIGHT_FIX)}",
            "verify_command": "python -m pytest -q",
        }
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=600)
    assert task["result"]["verification_passed"] is True

    pod = json.loads(
        kubectl("get", "pod", "-n", NAMESPACE, "-l", f"sca.task_id={task_id}", "-o", "json")
    )["items"][0]["spec"]
    assert pod["nodeName"] == worker_node
    assert all("hostPath" not in v for v in pod["volumes"])

    # A second turn in the same sandbox, then the task's state is in the object store.
    seq = (await stack.events(task_id))[-1]["seq"]
    await stack.review(task_id, "request_changes", f"Add sub.\n{block(ADD_SUB)}")
    await stack.wait_for(task_id, "AWAITING_REVIEW", since_seq=seq, timeout=600)
    patch = await stack.patch(task_id)
    assert "+    return a + b" in patch and "def sub" in patch
    objects = stack.settings.objects_dir() / "tasks" / task_id / "state"
    assert (objects / "manifest.json").exists()

    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED", timeout=300)
    remaining = kubectl("get", "pod", "-n", NAMESPACE, "-l", f"sca.task_id={task_id}", "-o", "name")
    assert not remaining.strip()
