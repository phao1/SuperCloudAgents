"""C3.8 with a real sandbox and a fixed model completion."""

import subprocess

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.integration]


@pytest.fixture(scope="module", autouse=True)
def docker_available():
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker unreachable")
    if result.returncode:
        pytest.skip("Docker unreachable")


@pytest.fixture(autouse=True)
def fake_gateway(monkeypatch):
    class Gateway:
        sandbox_url = "http://unused/v1"

        async def issue_key(self, *args, **kwargs):
            return "fake-virtual-key"

        async def revoke_key(self, *args):
            return None

        async def complete(self, messages, model):
            return '[{"text":"Use pnpm for this repository","category":"command"}]'

    async def make_gateway(*args):
        return Gateway()

    monkeypatch.setattr("sca.orchestration.worker.make_gateway", make_gateway)


async def test_review_memory_reused_and_isolated(stack, source_repo):
    from .conftest import git

    instruction = """Fix the bug.
```sca-fake
sed -i 's/return a - b/return a + b/' calc.py
mkdir -p .sca
printf '%s\\n' '- [pitfall] Run tests before publishing' > .sca/memory-proposals.md
```
```sca-fake
printf '\\n# reviewed\\n' >> calc.py
```
"""
    first = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": instruction,
            "verify_command": "python -m pytest -q",
        }
    )
    pending = await stack.wait_for(first, "AWAITING_REVIEW")
    agent_ids = pending["result"]["memory_proposals"]
    assert agent_ids
    seq = (await stack.events(first))[-1]["seq"]
    await stack.review(first, "request_changes", "Use pnpm for this repository")
    await stack.wait_for(first, "AWAITING_REVIEW", since_seq=seq)
    r = await stack.http.post(
        f"/v1/tasks/{first}/review", json={"decision": "approve", "accept_memories": agent_ids}
    )
    assert r.status_code == 200, r.text
    await stack.wait_for(first, "SUCCEEDED")
    # SUCCEEDED precedes the post-success distillation activity.
    import asyncio

    async with asyncio.timeout(30):
        while True:
            proposals = (await stack.http.get("/v1/memories", params={"status": "proposed"})).json()
            distilled = [
                m
                for m in proposals
                if m["source_task_id"] == first and m["source"] == "reviewer_feedback"
            ]
            if distilled:
                break
            await asyncio.sleep(0.2)
    r = await stack.http.patch("/v1/memories/" + distilled[0]["id"], json={"status": "active"})
    assert r.status_code == 200
    check = """Use the saved conventions.
```sca-fake
grep -q 'Use pnpm' .sca/context/MEMORY.md && printf 'memory used' > result.txt
```
"""
    second = await stack.submit(
        {"repo_url": str(source_repo), "instruction": check, "verify_command": "test -f result.txt"}
    )
    pending = await stack.wait_for(second, "AWAITING_REVIEW")
    assert pending["result"]["verification_passed"]
    assert any(
        e["type"] == "memory.injected" and distilled[0]["id"] in e["payload"]["ids"]
        for e in await stack.events(second)
    )
    await stack.review(second, "approve")
    await stack.wait_for(second, "SUCCEEDED")
    other_repo = source_repo.parent / "other.git"
    git(source_repo.parent, "clone", "-q", "--bare", str(source_repo), str(other_repo))
    isolated = """Check isolation.
```sca-fake
! grep -q 'Use pnpm' .sca/context/MEMORY.md && touch isolated
```
"""
    for tenant, repo in [("default", other_repo), ("other-tenant", source_repo)]:
        if tenant == "default":
            task_id = await stack.submit(
                {
                    "repo_url": str(repo),
                    "instruction": isolated,
                    "verify_command": "test -f isolated",
                }
            )
        else:
            # The shared e2e token is scoped to default; create the other tenant's task through
            # the store and start its ordinary workflow, then inspect via the worker store.
            from sca.orchestration.client import TemporalOrchestrator

            task, _ = await stack.store.create_task(
                tenant_id=tenant,
                kind="coding",
                spec={
                    "repo_url": str(repo),
                    "instruction": isolated,
                    "verify_command": "test -f isolated",
                },
                idempotency_key=None,
            )
            from temporalio.client import Client

            client = await Client.connect(stack.settings.temporal_address)
            await TemporalOrchestrator(client, stack.settings.task_queue).start_task(
                str(task.id), "coding"
            )
            stack.submitted.append(str(task.id))
            async with asyncio.timeout(90):
                while (await stack.store.get_task(task.id)).state != "AWAITING_REVIEW":
                    assert (await stack.store.get_task(task.id)).state not in (
                        "FAILED",
                        "CANCELLED",
                    )
                    await asyncio.sleep(0.3)
            isolated_task = await stack.store.get_task(task.id)
            assert isolated_task.result["verification_passed"]
            assert not isolated_task.result["memory_context"]
            continue
        await stack.wait_for(task_id, "AWAITING_REVIEW")
        isolated_task = await stack.task(task_id)
        assert isolated_task["result"]["verification_passed"]
        assert not isolated_task["result"]["memory_context"]
