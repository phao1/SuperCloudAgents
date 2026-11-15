"""Phase 1 acceptance test: the real Codex agent, including one review round.

Spends model tokens. Run with `make e2e-codex`.
"""

from __future__ import annotations

import pytest

from .conftest import git, sandbox_running

pytestmark = [pytest.mark.e2e, pytest.mark.codex]


@pytest.fixture
def agent_backend() -> str:
    return "codex"


async def test_codex_fixes_bug_then_applies_review_feedback(stack, source_repo):
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": "The test suite fails because calc.add is wrong. Fix calc.add.",
            "verify_command": "python -m pytest -q",
            "push": True,
            "turn_timeout_s": 600,
        }
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=900)
    assert task["result"]["verification_passed"] is True
    assert "calc.py" in await stack.patch(task_id)

    events = await stack.events(task_id)
    await stack.review(
        task_id,
        "request_changes",
        "Also add a function multiply(a, b) to calc.py, with a test for it in test_calc.py.",
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=900, since_seq=events[-1]["seq"])
    assert task["result"]["verification_passed"] is True
    patch = await stack.patch(task_id)
    assert "def multiply" in patch and "test_calc.py" in patch

    await stack.review(task_id, "approve")
    task = await stack.wait_for(task_id, "SUCCEEDED")

    branch = task["result"]["branch"]
    assert "def multiply" in git(source_repo, "show", f"{branch}:calc.py")
    assert task["usage"].get("input_tokens", 0) > 0
    assert not sandbox_running(task_id)

    events = await stack.events(task_id)
    types = {e["type"] for e in events}
    assert {"agent.message", "agent.command", "agent.usage", "agent.turn_completed"} <= types
    # The second turn resumed the first turn's session rather than starting cold.
    assert len([e for e in events if e["type"] == "agent.turn_started"]) == 2
    print("\nusage:", task["usage"], "\nevents:", len(events))
