"""Planning scripts are embedded in the task instruction passed to the planner."""

from __future__ import annotations

import json
import shlex
from datetime import datetime

import pytest

from .test_e2e_fake import RIGHT_FIX, block
from .test_e2e_subtasks import require_docker  # noqa: F401 — reuse service availability fixture

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("overlap", [False, True])
async def test_automatic_split_or_fallback(stack, source_repo, overlap):
    extra = "printf 'VALUE = 42\\n' > extra.py"
    plan = {
        "split": True,
        "reason": "Independent files",
        "subtasks": [
            {"name": "add", "instruction": "Fix calc.add.", "paths": ["calc.py"]},
            {
                "name": "extra",
                "instruction": "Add extra.py with VALUE = 42.",
                "paths": ["calc.py"] if overlap else ["extra.py"],
            },
        ],
    }
    # Every agent turn of this task (planning, each subtask, a fallback single agent) is given
    # the task's instruction, so the scripted agent runs this one block everywhere; it decides
    # what to do from the branch it is on.
    script = (
        'case "$(git branch --show-current)" in '
        "*-plan-*) mkdir -p .sca; printf '%s' "
        + shlex.quote(json.dumps(plan))
        + " > .sca/plan.json ;; "
        f"*-extra) sleep 5; {extra} ;; "
        f"*) sleep 5; {RIGHT_FIX} ;; esac"
    )
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": "Fix addition and add extra module.\n" + block(script),
            "plan": "auto",
            "max_fix_attempts": 0,
            "verify_command": 'case "$(git branch --show-current)" in '
            "*-extra) python -c 'from extra import VALUE; assert VALUE == 42' ;; "
            "*) python -m pytest -q ;; esac",
        }
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW")
    assert task["result"]["verification_passed"] is True
    events = await stack.events(task_id)
    assert any(e["type"] == "plan.started" for e in events)
    planner = next(
        e["payload"] for e in events
        if e["type"] == "sandbox.created" and e["payload"].get("subtask") == "plan-1"
    )
    assert planner["driver"] == stack.settings.sandbox_driver
    assert planner["state_transport"] == stack.settings.state_transport
    assert planner["image"] and planner["cpus"] > 0 and planner["memory_mb"] > 0
    assert planner["task_queue"] == stack.settings.task_queue and planner["worker"]
    if planner["driver"] == "docker":
        assert len(planner["container_id"]) == 64
    patch = await stack.patch(task_id)
    assert "return a + b" in patch
    if overlap:
        rejected = next(e for e in events if e["type"] == "plan.rejected")
        assert "overlapping" in rejected["payload"]["reason"]
        assert "subtasks" not in task["result"]
    else:
        assert [s["status"] for s in task["result"]["subtasks"]] == ["merged", "merged"]
        assert "extra.py" in patch
        completed = next(e for e in events if e["type"] == "plan.completed")
        assert completed["payload"]["split"] is True
        response = await stack.http.get(f"/v1/tasks/{task_id}/artifacts/plan.json")
        assert response.status_code == 200 and response.json() == plan
        started = [
            e
            for e in events
            if e["type"] == "agent.turn_started" and e["payload"].get("subtask") in {"add", "extra"}
        ]
        finished = [
            e
            for e in events
            if e["type"] == "agent.turn_completed"
            and e["payload"].get("subtask") in {"add", "extra"}
        ]
        assert len(started) == len(finished) == 2
        assert {row["payload"]["workflow_id"] for row in started} == {
            f"task-{task_id}-sub-add", f"task-{task_id}-sub-extra",
        }
        assert all(row["payload"]["worker"] for row in started)
        assert max(datetime.fromisoformat(e["created_at"]) for e in started) < min(
            datetime.fromisoformat(e["created_at"]) for e in finished
        )
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")


@pytest.fixture
def agent_backend(request):
    return "codex" if request.node.get_closest_marker("codex") else "fake"


@pytest.mark.codex
async def test_real_model_can_choose_whether_to_split(stack, source_repo):
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "plan": "auto",
            "instruction": "Fix the addition bug in calc.py and add an independent greeting.py "
            "module whose greet(name) returns 'Hello, ' + name, with its own test.",
            "verify_command": "python -m pytest -q",
        }
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=600)
    assert task["result"]["verification_passed"] is True
    plans = [
        e["payload"]
        for e in await stack.events(task_id)
        if e["type"] in {"plan.completed", "plan.rejected"}
    ]
    print(json.dumps(plans, ensure_ascii=False))
    assert plans
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")
