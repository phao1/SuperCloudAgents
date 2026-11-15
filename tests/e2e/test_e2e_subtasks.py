"""Parallel child workflows with the real sandbox and scripted agent."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime

import pytest

from .conftest import sandbox_running
from .test_e2e_fake import RIGHT_FIX, block

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def require_docker():
    if not shutil.which("docker"):
        pytest.skip("Docker unavailable")
    try:
        available = (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=10, check=False
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        available = False
    if not available:
        pytest.skip("Docker unreachable")


async def test_parallel_subtasks_merge(stack, source_repo):
    extra = (
        "printf 'VALUE = 42\n' > extra.py; "
        "printf 'from extra import VALUE\ndef test_extra():\n    assert VALUE == 42\n' > test_extra.py"
    )
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": "Fix addition and add extra module.",
            # The unrelated child inherits buggy add. The merged parent checks both modules.
            "verify_command": 'case "$(git branch --show-current)" in '
            "*-extra) python -m pytest -q test_extra.py ;; *) python -m pytest -q ;; esac",
            "max_fix_attempts": 0,
            "subtasks": [
                {"name": "add", "instruction": block(f"sleep 5; {RIGHT_FIX}")},
                {"name": "extra", "instruction": block(f"sleep 5; {extra}")},
            ],
        }
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW")
    assert task["result"]["verification_passed"] is True
    assert [s["status"] for s in task["result"]["subtasks"]] == ["merged", "merged"]
    patch = await stack.patch(task_id)
    assert "return a + b" in patch and "test_extra.py" in patch and "extra.py" in patch
    events = await stack.events(task_id)
    start = next(
        e for e in events if e["type"] == "task.state_changed" and e["payload"]["to"] == "RUNNING"
    )
    merged = next(e for e in events if e["type"] == "subtasks.merged")
    elapsed = (
        datetime.fromisoformat(merged["created_at"]) - datetime.fromisoformat(start["created_at"])
    ).total_seconds()
    assert elapsed < 9, elapsed
    assert not [
        e for e in events if e["type"] == "agent.turn_started" and "subtask" not in e["payload"]
    ]
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")
    # State is recorded before the final cleanup activity completes.
    from .test_e2e_verification import wait_event

    await wait_event(stack, task_id, "sandbox.destroyed", count=3)
    assert not sandbox_running(task_id)


async def test_conflict_patch_and_feedback(stack, source_repo):
    other = "printf 'def add(a, b):\\n    return b + a\\n' > calc.py"
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": "Fix addition.",
            "verify_command": "python -m pytest -q",
            "max_fix_attempts": 0,
            "subtasks": [
                {"name": "one", "instruction": block(RIGHT_FIX)},
                {"name": "two", "instruction": block(other)},
            ],
        }
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW")
    assert task["result"]["verification_passed"] is False
    assert [s["status"] for s in task["result"]["subtasks"]] == ["merged", "conflict"]
    response = await stack.http.get(f"/v1/tasks/{task_id}/artifacts/subtask-two.patch")
    assert response.status_code == 200 and "return b + a" in response.text
    last = (await stack.events(task_id))[-1]["seq"]
    await stack.review(
        task_id, "request_changes", block("test -f /workspace/.sca/subtask-two.patch; " + RIGHT_FIX)
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW", since_seq=last)
    assert task["result"]["verification_passed"] is True
    assert ".sca/" not in await stack.patch(task_id)
    events = await stack.events(task_id)
    parent_turn = next(
        e for e in events if e["type"] == "agent.turn_started" and "subtask" not in e["payload"]
    )
    assert "two" in parent_turn["payload"]["prompt"]
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")
    from .test_e2e_verification import wait_event

    await wait_event(stack, task_id, "sandbox.destroyed", count=3)
    assert not sandbox_running(task_id)
