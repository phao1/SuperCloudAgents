"""Phase 2, components 6-8: parallel checks, external CI, budgets, caches. Real stack, fake model."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from .conftest import git
from .test_e2e_fake import RIGHT_FIX, block

pytestmark = pytest.mark.e2e


async def wait_event(stack, task_id: str, type_: str, count: int = 1, timeout: float = 120):
    async with asyncio.timeout(timeout):
        while True:
            found = [e for e in await stack.events(task_id) if e["type"] == type_]
            if len(found) >= count:
                return found[count - 1]
            await asyncio.sleep(0.5)


async def test_checks_run_in_parallel_and_all_must_pass(stack, source_repo):
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": f"Fix add.\n{block(RIGHT_FIX)}\n{block('touch lint-ok')}",
            "verify_command": "sleep 4 && python -m pytest -q",
            "verify_commands": ["sleep 4 && test -f lint-ok", "sleep 4 && true"],
        }
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW")
    assert task["result"]["verification_passed"] is True and task["result"]["turns"] == 2
    events = await stack.events(task_id)
    first, second = [e for e in events if e["type"] == "verification.completed"]

    # Turn 1: tests pass, the lint check does not; the task is judged on all of them.
    assert first["payload"]["passed"] is False
    assert [c["passed"] for c in first["payload"]["checks"]] == [True, False, True]
    assert "test -f lint-ok" in first["payload"]["output_tail"]
    assert "pytest" not in first["payload"]["output_tail"]  # only failures are fed back
    assert [c["passed"] for c in second["payload"]["checks"]] == [True, True, True]

    # Three 4-second checks took about 4 seconds, not 12.
    started = next(
        e for e in events
        if e["type"] == "task.state_changed" and e["payload"]["to"] == "VERIFYING"
    )
    elapsed = (
        datetime.fromisoformat(first["created_at"]) - datetime.fromisoformat(started["created_at"])
    ).total_seconds()
    print(f"\n3 checks of 4 s each finished in {elapsed:.1f} s")
    assert 4 <= elapsed < 8

    log = await stack.http.get(f"/v1/tasks/{task_id}/artifacts/verify-turn1.log")
    assert log.text.count("=====") == 6
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")


def run_ci(source_repo: Path, branch: str, commit: str, workdir: Path) -> tuple[bool, str]:
    """A stand-in CI pipeline: check out what was pushed and really run the project's checks,
    plus one rule the task's own verification does not know about."""
    checkout = workdir / f"ci-{commit[:8]}"
    git(workdir, "clone", "-q", "--branch", branch, str(source_repo), str(checkout))
    assert git(checkout, "rev-parse", "HEAD") == commit
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=checkout, capture_output=True, text=True, check=False,
    )
    if tests.returncode != 0:
        return False, tests.stdout[-2000:]
    if not (checkout / "CHANGELOG.md").exists():
        return False, "changelog check: CHANGELOG.md is missing"
    return True, "all checks passed"


async def test_external_ci_drives_the_task(stack, source_repo, tmp_path):
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": (
                f"Fix add.\n{block(RIGHT_FIX)}\n{block('echo fixed add > CHANGELOG.md')}"
            ),
            "verify_command": "python -m pytest -q",
            "ci": {"timeout_s": 120},
        }
    )
    for round_, expected in enumerate([False, True], start=1):
        request = (await wait_event(stack, task_id, "ci.requested", round_))["payload"]
        assert (await stack.task(task_id))["state"] == "VERIFYING"
        passed, summary = await asyncio.to_thread(
            run_ci, source_repo, request["branch"], request["commit"], tmp_path
        )
        assert passed is expected, summary
        r = await stack.http.post(
            f"/v1/tasks/{task_id}/ci-result",
            json={
                "commit": request["commit"], "passed": passed, "summary": summary,
                "url": f"http://ci.example/{round_}",
            },
        )
        assert r.status_code == 200, r.text

    task = await stack.wait_for(task_id, "AWAITING_REVIEW")
    assert task["result"]["verification_passed"] is True and task["result"]["turns"] == 2
    events = await stack.events(task_id)
    assert [e["payload"]["passed"] for e in events if e["type"] == "ci.completed"] == [False, True]
    assert "CHANGELOG.md" in await stack.patch(task_id)
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")


async def test_task_over_its_token_budget_is_stopped(stack, source_repo):
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": f"Never right.\n{block('true')}",
            "verify_command": "false",
            "max_fix_attempts": 5,
            "max_tokens": 3,  # the scripted agent reports 2 tokens a turn
        }
    )
    task = await stack.wait_for(task_id, "FAILED")
    assert "budget exceeded" in task["state_reason"]
    events = await stack.events(task_id)
    assert len([e for e in events if e["type"] == "agent.turn_started"]) == 2
    assert any(e["type"] == "budget.exceeded" for e in events)


async def test_tenant_daily_budget_stops_tasks(stack, source_repo):
    await stack.store.set_tenant_budget("default", 1)
    try:
        task_id = await stack.submit(
            {"repo_url": str(source_repo), "instruction": f"x\n{block('true')}",
             "verify_command": "false", "max_fix_attempts": 5}
        )
        task = await stack.wait_for(task_id, "FAILED")
        assert "budget exceeded" in task["state_reason"]
    finally:
        await stack.store.set_tenant_budget("default", None)


async def test_caches_are_used_and_kept_per_tenant(stack, source_repo):
    spec = {
        "repo_url": str(source_repo),
        "instruction": f"Fix add.\n{block(RIGHT_FIX)}",
        "verify_command": "python -m pytest -q && test \"$PIP_CACHE_DIR\" = /cache/pip "
        "&& touch /cache/pip/writable",
    }
    for _ in range(2):  # the second task builds its workspace from the cached mirror
        task_id = await stack.submit(spec)
        task = await stack.wait_for(task_id, "AWAITING_REVIEW")
        assert task["result"]["verification_passed"] is True
        await stack.review(task_id, "approve")
        await stack.wait_for(task_id, "SUCCEEDED")
    cache = stack.settings.data_dir.resolve() / "cache"
    assert len(list((cache / "repos").glob("*.git"))) == 1
    assert (cache / "pip" / "default" / "writable").exists()
