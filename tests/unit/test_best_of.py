import asyncio
import io
import json
import tarfile
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError
from temporalio import activity, workflow
from temporalio.testing import ActivityEnvironment
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from sca.core.errors import WorkspaceError
from sca.core.models import TaskState
from sca.kernel import activities as kernel_activities
from sca.kernel.workflow import TaskWorkflow
from sca.kinds.coding import activities as coding_activities
from sca.kinds.coding.best_of import Candidate, DiffStats, allocate_attempts, choose_candidate
from sca.kinds.coding.kind import CodingKind
from sca.kinds.coding.spec import CodingSpec
from sca.kinds.coding.workflow import CodingSubtaskWorkflow, CodingTaskWorkflow
from sca.storage.local import key_for
from sca.workspace.snapshots import SnapshotStore
from sca.workspace.tarsafe import chunks
from tests.best_of_harness import TASK_ID, api_client, make_harness


def spec(**kwargs) -> CodingSpec:
    return CodingSpec(repo_url="repo", instruction="Fix it", **kwargs)


def test_attempts_round_robin_and_dollar_and_token_shares():
    task = spec(max_cost_usd=1, max_tokens=7, best_of={
        "n": 3, "models": ["m1", "m2"], "agent_versions": ["2.3.0-sca.1", "2.3.0-sca.2"],
    })
    attempts = allocate_attempts(task, "default")
    assert [a.model for a in attempts] == ["m1", "m2", "m1"]
    assert [a.agent_version for a in attempts] == ["2.3.0-sca.1", "2.3.0-sca.2", "2.3.0-sca.1"]
    assert [a.max_cost_usd for a in attempts] == [.333334, .333333, .333333]
    assert [a.max_tokens for a in attempts] == [3, 2, 2]
    task = spec(model="task-model", agent_version="2.3.0-sca.3", max_cost_usd=.3, best_of={
        "n": 2, "per_attempt_max_cost_usd": [.1, .2],
    })
    attempts = allocate_attempts(task, "default")
    assert [(a.model, a.agent_version, a.max_cost_usd) for a in attempts] == [
        ("task-model", "2.3.0-sca.3", .1), ("task-model", "2.3.0-sca.3", .2),
    ]
    attempts = allocate_attempts(spec(best_of={"n": 2}), "default")
    assert all(a.model == "default" and a.max_cost_usd is None for a in attempts)


@pytest.mark.parametrize("options", [
    {"best_of": {"n": 1}},
    {"best_of": {"n": 6}},
    {"best_of": {"n": 2, "models": []}},
    {"best_of": {"n": 2, "agent_versions": ["../invalid"]}},
    {"best_of": {"n": 2, "per_attempt_max_cost_usd": [.1]}},
    {"best_of": {"n": 2, "per_attempt_max_cost_usd": [.1, .3]}, "max_cost_usd": .3},
    {"best_of": {"n": 2}, "plan": "auto"},
    {"best_of": {"n": 2}, "subtasks": [{"name": n, "instruction": n} for n in ["a", "b"]]},
])
def test_rejects_invalid_best_of_specs(options):
    with pytest.raises(ValidationError):
        spec(**options)


def candidate(attempt: int, checks: list[bool], cost: float | None, lines: int) -> Candidate:
    verification = {"passed": all(checks), "checks": [{"passed": check} for check in checks]}
    return Candidate(attempt, "test", None, f"commit-{attempt}", verification, cost, 1,
                     DiffStats(1, lines, 0), f"candidate-{attempt}.patch")


@pytest.mark.parametrize("a,b,winner", [
    (candidate(1, [True], .4, 2), candidate(2, [False], .1, 1), 1),
    (candidate(1, [True], .4, 1), candidate(2, [True], .2, 3), 2),
    (candidate(1, [True], .2, 3), candidate(2, [True], .2, 1), 2),
    (candidate(1, [False, False], .1, 1), candidate(2, [True, False], .4, 3), 2),
    (candidate(1, [True, False], .2, 1), candidate(2, [False, True], .1, 3), 2),
    (candidate(1, [True], None, 1), candidate(2, [True], .1, 3), 2),
    (candidate(1, [True], .2, 1), candidate(2, [True], .2, 1), 1),
])
def test_selection_rules(a, b, winner):
    assert choose_candidate([b, a]).attempt == winner
    assert choose_candidate([replace(a, patch_artifact=None)]) is None


class LocalWorkflow:
    """Calls the same activity definitions and child workflow in an ActivityEnvironment."""

    def __init__(self, monkeypatch, harness, task=None) -> None:
        self.task = task or CodingTaskWorkflow()
        self.activities = {activity._Definition.must_from_callable(a).name: a for a in harness.activities}
        self.signal = asyncio.Event()
        monkeypatch.setattr(workflow, "patched", lambda name: True)
        monkeypatch.setattr(workflow, "execute_activity", self.execute_activity)
        monkeypatch.setattr(workflow, "start_activity", self.start_activity)
        monkeypatch.setattr(workflow, "execute_child_workflow", self.execute_child)
        monkeypatch.setattr(workflow, "wait_condition", self.wait_condition)

    async def execute_activity(self, name, *arguments, args=None, **kwargs):
        return await ActivityEnvironment().run(self.activities[name], *(args if args is not None else arguments))

    def start_activity(self, name, arg, **kwargs):
        return asyncio.create_task(self.execute_activity(name, arg, **kwargs))

    async def execute_child(self, method, *, args, **kwargs):
        assert method == CodingSubtaskWorkflow.run
        return await CodingSubtaskWorkflow().run(*args)

    async def wait_condition(self, condition, timeout=None):
        async with asyncio.timeout(timeout.total_seconds() if timeout else None):
            while not condition():
                await self.signal.wait()
                self.signal.clear()

    async def submit_review(self, task_id, decision, feedback):
        await self.task.submit_review(decision, feedback)
        self.signal.set()

    async def run(self, *, timeout_s: float = 20):
        async with asyncio.timeout(timeout_s):
            return await self.task.run(str(TASK_ID))


@pytest.mark.parametrize("driver,transport", [("docker", "mount"), ("k8s", "sync")])
async def test_parallel_forks_with_real_git_and_independent_budgets(tmp_path, best_of_repo, monkeypatch, driver, transport):
    harness = make_harness(tmp_path, best_of_repo, concurrency=3, driver=driver, transport=transport,
                           push=True, auto_finish=True, max_cost_usd=.9, best_of={
                               "n": 3, "models": ["wide", "cheap", "partial"],
                               "agent_versions": ["2.3.0-sca.1", "2.3.0-sca.2"],
                           }, max_fix_attempts=0)
    assert await LocalWorkflow(monkeypatch, harness).run() == "SUCCEEDED"
    assert len(harness.sandbox.setup_calls) == 1
    assert len(harness.sandbox.checks) == 6
    starts = sorted(harness.agent.started, key=lambda entry: entry[0].task_id)
    assert [ctx.model for ctx, _, _ in starts] == ["wide", "cheap", "partial"]
    assert [ctx.agent_version for ctx, _, _ in starts] == ["2.3.0-sca.1", "2.3.0-sca.2", "2.3.0-sca.1"]
    assert len({ctx.task_id for ctx, _, _ in starts}) == len({box for _, box, _ in starts}) == 3
    assert {session for _, _, session in starts} == {None}
    assert sorted(harness.gateway.keys.values()) == [("cheap", .3), ("partial", .3), ("wide", .3)]
    assert not harness.sandbox.instances
    assert harness.store.task.usage["cost_micro_usd"] == 350_000
    assert harness.store.task.result["best_of_chosen"] == 2
    assert harness.store.task.result["pushed"] is True
    commit = harness.store.task.result["commit"]
    assert (await best_of_repo._git("rev-parse", harness.store.task.result["branch"])).strip() == commit
    assert (harness.coding._workspace(str(TASK_ID)).workspace / "app.txt").read_text() == "repaired\n"
    patch = await harness.objects.get(key_for(TASK_ID, "changes.patch"))
    assert patch == await harness.objects.get(key_for(TASK_ID, "best-of-2.patch"))
    assert b"node_modules" not in patch
    events = [(name, payload) for name, payload in harness.store.events if name.startswith("best_of.")]
    assert [event for event, _ in events] == ["best_of.started", *["best_of.attempt_completed"] * 3,
                                              "best_of.selected"]
    assert [payload["attempt"] for event, payload in events if event == "best_of.selected"] == [2]


async def test_attempt_budget_exhaustion_stops_fix_spend_and_keeps_verification(tmp_path, best_of_repo, monkeypatch):
    harness = make_harness(tmp_path, best_of_repo, concurrency=2, auto_finish=True, max_cost_usd=.15,
                           max_fix_attempts=3, best_of={"n": 2, "models": ["partial", "cheap"],
                                                       "per_attempt_max_cost_usd": [.05, .1]})
    assert await LocalWorkflow(monkeypatch, harness).run() == "SUCCEEDED"
    task = harness.store.task
    assert task.result["best_of_chosen"] == 2
    assert task.usage["cost_micro_usd"] == 150_000
    assert len(harness.gateway.keys) == 2
    partial = task.result["best_of_candidate_1"]
    assert partial["turns"] == 1 and partial["verification"]["passed"] is False
    assert "budget exhausted" in partial["reason"]
    assert task.result["best_of_candidate_2"]["verification"]["passed"] is True


async def test_each_attempt_uses_its_own_turn_limit(tmp_path, best_of_repo, monkeypatch):
    harness = make_harness(tmp_path, best_of_repo, concurrency=2, auto_finish=True, max_fix_attempts=1,
                           use_gateway=False, best_of={"n": 2, "models": ["partial"]})
    assert await LocalWorkflow(monkeypatch, harness).run() == "FAILED"
    assert len(harness.sandbox.setup_calls) == 1
    assert len(harness.sandbox.checks) == 8
    for name in ("best-of-1", "best-of-2"):
        starts = [(ctx, session) for ctx, _, session in harness.agent.started if ctx.task_id.endswith(name)]
        assert len(starts) == 2
        assert starts[0][1] is None and starts[1][1] == f"fake-{starts[0][0].task_id}"
    assert harness.store.task.result["best_of_chosen"] == 1
    patch = await harness.objects.get(key_for(TASK_ID, "changes.patch"))
    assert b"+unfinished" in patch


async def test_human_selection_uses_review_roles_and_preserves_chosen_failed_patch(tmp_path, best_of_repo, monkeypatch):
    harness = make_harness(tmp_path, best_of_repo, concurrency=2, max_fix_attempts=0, use_gateway=False,
                           best_of={"n": 2, "models": ["cheap", "partial"], "select": "human"})
    local = LocalWorkflow(monkeypatch, harness)
    base = f"/v1/tasks/{TASK_ID}"
    task = asyncio.create_task(local.run())
    try:
        async with asyncio.timeout(15), api_client(harness, local) as api:
            ready = harness.store.states[TaskState.AWAITING_REVIEW]
            await ready.wait()
            ready.clear()
            response = await api.get(f"{base}/best-of")
            assert response.status_code == 200
            view = response.json()
            assert view["awaiting_selection"] and view["chosen_attempt"] is None
            assert len(view["attempts"]) == 2
            failed = view["attempts"][1]
            assert failed["verification"]["checks"][1]["passed"] is True
            assert failed["diff_stats"]["changed_lines"] == 2
            assert failed["cost_usd"] is None
            selected_patch = (await api.get(failed["patch_url"])).content
            assert b"+unfinished" in selected_patch
            assert (await api.get(f"{base}/best-of", headers={"Authorization": "Bearer other"})).status_code == 404
            endpoint = f"{base}/best-of/select"
            assert (await api.post(endpoint, json={"attempt": 2}, headers={"Authorization": "Bearer viewer"})).status_code == 403
            assert (await api.post(endpoint, json={"attempt": 3})).status_code == 422
            assert (await api.post(f"{base}/review", json={"decision": "approve"})).status_code == 409
            assert (await api.post(endpoint, json={"attempt": 2})).status_code == 202
            await ready.wait()
            assert (await api.get(f"{base}/artifacts/changes.patch")).content == selected_patch
            view = (await api.get(f"{base}/best-of")).json()
            assert view["chosen_attempt"] == 2 and view["attempts"][1]["chosen"] is True
            assert not view["awaiting_selection"]
            assert harness.store.task.result["verification_passed"] is False
            assert (await api.post(endpoint, json={"attempt": 1})).status_code == 409
            ready.clear()
            review = {"decision": "request_changes", "feedback": "Finish the repair"}
            assert (await api.post(f"{base}/review", json=review)).status_code == 200
            await ready.wait()
            assert harness.store.task.result["verification_passed"] is True
            assert b"+repaired" in (await api.get(f"{base}/artifacts/changes.patch")).content
            ctx, _, session = harness.agent.started[-1]
            assert ctx.task_id == str(TASK_ID) and session is None
            assert (await api.post(f"{base}/review", json={"decision": "approve"})).status_code == 200
            assert await task == "SUCCEEDED"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    event = next(payload for name, payload in harness.store.events if name == "best_of.selected")
    assert event["actor"] == "reviewer"


async def test_normal_tasks_emit_identical_event_payloads(tmp_path, best_of_repo, monkeypatch):
    legacy = make_harness(tmp_path / "prev", best_of_repo, auto_finish=True)
    current = make_harness(tmp_path / "next", best_of_repo, auto_finish=True, best_of=None)
    assert "best_of" not in current.store.task.spec and "fork_from_snapshot" not in current.store.task.spec
    clock = SimpleNamespace(time=lambda: 10, monotonic=lambda: 10)
    monkeypatch.setattr(kernel_activities, "time", clock)
    monkeypatch.setattr(coding_activities, "time", clock)
    assert await LocalWorkflow(monkeypatch, legacy, TaskWorkflow()).run() == "SUCCEEDED"
    assert await LocalWorkflow(monkeypatch, current).run() == "SUCCEEDED"
    assert json.dumps(current.store.events) == json.dumps(legacy.store.events)


async def test_top_level_snapshot_flag_uses_existing_prepared_cache(tmp_path, best_of_repo):
    harness = make_harness(tmp_path, best_of_repo, fork_from_snapshot=True)
    assert harness.store.task.spec["fork_from_snapshot"] is True
    harness.coding.settings.workspace_snapshots = True
    env = ActivityEnvironment()
    task_id = str(TASK_ID)
    await env.run(harness.coding.prepare_workspace, task_id)
    await env.run(harness.kernel.create_sandbox, task_id)
    await env.run(harness.kernel.run_setup, task_id)
    assert harness.store.task.result["snapshot_id"]
    assert any(name == "workspace.snapshotted" for name, _ in harness.store.events)
    other = make_harness(tmp_path, best_of_repo, fork_from_snapshot=True)
    other.store.task.id = UUID("87e973a1-6295-44e4-a3a3-d5a5094c67ad")
    other.coding.settings.workspace_snapshots = True
    second_id = str(other.store.task.id)
    await env.run(other.coding.prepare_workspace, second_id)
    await env.run(other.kernel.create_sandbox, second_id)
    await env.run(other.kernel.run_setup, second_id)
    assert not other.sandbox.setup_calls
    assert (other.coding._workspace(second_id).workspace / "node_modules/fixture").is_file()
    assert other.store.task.result["snapshot_id"] == harness.store.task.result["snapshot_id"]
    await env.run(harness.kernel.destroy_sandbox, task_id)
    await env.run(other.kernel.destroy_sandbox, second_id)


async def test_best_of_workflows_import_in_temporal_sandbox():
    runner = SandboxedWorkflowRunner()
    for cls in CodingKind().workflows():
        runner.prepare_workflow(workflow._Definition.must_from_class(cls))


async def test_snapshot_rejects_link_that_would_escape_after_fork(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file").write_text("fixture")
    (workspace / "link").symlink_to("../workspace/file")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        archive.add(workspace, arcname="workspace")
    stream.seek(0)
    snapshots = SnapshotStore(tmp_path / "snapshots")
    with pytest.raises(WorkspaceError, match="symlink leaves"):
        await snapshots.publish("a" * 64, chunks(stream), base_commit="base", saved_seconds=0,
                                archive_root="workspace")
