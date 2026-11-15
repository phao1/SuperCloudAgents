"""End to end with the scripted agent: everything real except the model."""

from __future__ import annotations

import pytest

from .conftest import git, sandbox_running

pytestmark = pytest.mark.e2e

WRONG_FIX = "printf 'def add(a, b):\\n    return a * b\\n' > calc.py"
RIGHT_FIX = "printf 'def add(a, b):\\n    return a + b\\n' > calc.py"
ADD_SUB = (
    "printf '\\n\\ndef sub(a, b):\\n    return a - b\\n' >> calc.py && "
    "printf '\\n\\ndef test_sub():\\n    assert calc.sub(5, 3) == 2\\n' >> test_calc.py"
)


def block(script: str) -> str:
    return f"```sca-fake\n{script}\n```"


async def test_fix_loop_review_round_and_push(stack, source_repo):
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": f"Fix add.\n{block(WRONG_FIX)}\n{block(RIGHT_FIX)}",
            "verify_command": "python -m pytest -q",
            "push": True,
        }
    )
    # Turn 1 applies the wrong fix, verification fails, turn 2 applies the right one.
    task = await stack.wait_for(task_id, "AWAITING_REVIEW")
    assert task["result"]["verification_passed"] is True
    assert task["result"]["turns"] == 2
    assert sandbox_running(task_id)
    patch = await stack.patch(task_id)
    assert "+    return a + b" in patch and "a * b" not in patch
    # The verify command ran pytest in a repo with no .gitignore; its caches stay out.
    assert "__pycache__" not in patch and ".pytest_cache" not in patch

    events = await stack.events(task_id)
    verifications = [e["payload"]["passed"] for e in events if e["type"] == "verification.completed"]
    assert verifications == [False, True]
    assert "assert" in next(
        e for e in events if e["type"] == "verification.completed"
    )["payload"]["output_tail"]

    # A reviewer asks for more; the same task continues.
    last_seq = events[-1]["seq"]
    await stack.review(task_id, "request_changes", f"Add sub too.\n{block(ADD_SUB)}")
    task = await stack.wait_for(task_id, "AWAITING_REVIEW", since_seq=last_seq)
    assert "def sub" in await stack.patch(task_id)
    assert task["result"]["verification_passed"] is True

    await stack.review(task_id, "approve")
    task = await stack.wait_for(task_id, "SUCCEEDED")

    # The branch was pushed back to the source repo with the final content.
    branch = task["result"]["branch"]
    assert task["result"]["pushed"] is True
    assert git(source_repo, "rev-parse", branch) == task["result"]["commit"]
    assert "def sub" in git(source_repo, "show", f"{branch}:calc.py")
    assert git(source_repo, "show", "main:calc.py").strip().endswith("return a - b")

    # The event log is complete and gap-free, and the sandbox is gone.
    events = await stack.events(task_id)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    states = [e["payload"]["to"] for e in events if e["type"] == "task.state_changed"]
    assert states == [
        "PROVISIONING", "RUNNING", "VERIFYING", "RUNNING", "VERIFYING", "AWAITING_REVIEW",
        "RUNNING", "VERIFYING", "AWAITING_REVIEW", "SUCCEEDED",
    ]
    assert events[-1]["type"] == "sandbox.destroyed" or any(
        e["type"] == "sandbox.destroyed" for e in events
    )
    assert not sandbox_running(task_id)
    assert task["usage"]


async def test_fix_attempts_exhausted_then_rejected(stack, source_repo):
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": f"Fix add.\n{block(WRONG_FIX)}",
            "verify_command": "python -m pytest -q",
            "max_fix_attempts": 1,
        }
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW")
    assert task["result"]["verification_passed"] is False
    assert task["result"]["pushed"] is False
    await stack.review(task_id, "reject", "still broken")
    task = await stack.wait_for(task_id, "CANCELLED")
    assert task["state_reason"] == "still broken"
    assert not sandbox_running(task_id)


async def test_bad_repo_fails_without_retrying(stack, tmp_path):
    task_id = await stack.submit(
        {"repo_url": str(tmp_path / "does-not-exist.git"), "instruction": "anything"}
    )
    task = await stack.wait_for(task_id, "FAILED", timeout=60)
    assert "prepare_workspace" in task["state_reason"]


async def test_cancel_running_task(stack, source_repo):
    task_id = await stack.submit(
        {"repo_url": str(source_repo), "instruction": f"Hang.\n{block('sleep 600')}"}
    )
    await stack.wait_for(task_id, "RUNNING")
    r = await stack.http.post(f"/v1/tasks/{task_id}/cancel")
    assert r.status_code == 200, r.text
    await stack.wait_for(task_id, "CANCELLED", timeout=60)
    assert not sandbox_running(task_id)
