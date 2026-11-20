import asyncio
import json
from datetime import UTC, datetime
from secrets import token_urlsafe
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from sca.agents.fake import FakeBackend
from sca.config import Settings
from sca.core.models import RepoCredential, Task, TaskState
from sca.kernel.activities import CheckpointResult, PublishInput, TurnResult, VerifyResult
from sca.kernel.kind import KindDeps
from sca.kinds.coding.activities import CodingActivities
from sca.kinds.coding.kind import workflow_spec
from sca.kinds.coding.spec import CodingSpec
from sca.kinds.coding.workflow import CodingTaskWorkflow
from sca.orchestration.worker import _kernel_activities
from sca.scm import service as scm_service
from sca.scm.provider import ScmError

_REPO = "https://github.com/acme/thing.git"
_PR_URL = "https://github.com/acme/thing/pull/41"


class MemoryStore:
    def __init__(self, task):
        self.task = task
        self.events = {}

    def for_tenant(self, tenant_id):
        assert tenant_id == self.task.tenant_id
        return self

    async def get_task(self, task_id):
        assert str(task_id) == str(self.task.id)
        return self.task.model_copy(deep=True)

    async def set_state(self, task_id, state, reason):
        self.task.state = state
        self.task.state_reason = reason
        return await self.get_task(task_id)

    async def append_event(self, task_id, kind, payload, dedup_key):
        assert str(task_id) == str(self.task.id)
        self.events.setdefault(dedup_key, (kind, payload))

    async def record_scm_pull_request(self, task_id, pr, *, opened=False):
        self.task.result["pr"] = pr
        if opened:
            await self.append_event(task_id, "pr.opened", pr, "pr.opened")
        return pr

    async def scm_check_results(self, task_id, turn):
        return {"verification.completed": {"checks": [
            {"passed": turn > 1, "timed_out": False, "exit_code": 0 if turn > 1 else 1},
        ]}}


class FakeForge:
    def __init__(self, store):
        self.store = store
        self.pull = None
        self.opened, self.edits, self.merges = [], [], []
        self.comments = {}
        self.fail_after_create = False
        self.merge_status = 200

    def handle(self, request):
        method = request.method
        path = request.url.path.removeprefix("/repos/acme/thing/")
        state = self.store.task.state
        if path == "pulls" and method == "GET":
            return httpx.Response(200, json=[self.pull] if self.pull else [])
        if path == "pulls" and method == "POST":
            self.opened.append(state)
            body = json.loads(request.content)
            self.pull = {
                "number": 41, "html_url": _PR_URL, "state": "open", "body": body["body"],
                "head": {"ref": body["head"], "repo": {"full_name": "acme/thing"}},
                "base": {"ref": body["base"]},
            }
            if self.fail_after_create:
                raise httpx.ReadTimeout("Response lost")
            return httpx.Response(201, json=self.pull)
        if path == "pulls/41" and method == "GET":
            return httpx.Response(200, json=self.pull)
        if path == "pulls/41" and method == "PATCH":
            self.pull["body"] = json.loads(request.content)["body"]
            self.edits.append(self.pull["body"])
            return httpx.Response(200, json=self.pull)
        if path == "pulls/41/merge" and method == "PUT":
            assert state == TaskState.AWAITING_REVIEW
            self.merges.append(json.loads(request.content))
            if self.merge_status == 200:
                self.pull.update(state="closed", merged=True)
            return httpx.Response(self.merge_status, json={"merged": self.merge_status == 200})
        if path in {"issues/41/comments", "issues/22/comments"}:
            rows = self.comments.setdefault(path, [])
            if method == "POST":
                rows.append({**json.loads(request.content), "task_state": state})
                return httpx.Response(201, json=rows[-1])
            assert method == "GET"
            return httpx.Response(200, json=rows)
        pytest.fail(f"Unexpected SCM request {method} {path}")


@pytest.fixture
def make_workflow(monkeypatch, tmp_path):
    def make(*, pr=None, decisions=(), issue=True, auto_finish=False, budget_reason=None):
        spec = CodingSpec(
            repo_url=_REPO, base_ref="main", instruction="Repair the calculation", push=True,
            pull_request=pr, memory_enabled=False, auto_finish=auto_finish, max_fix_attempts=0,
        ).model_dump()
        now = datetime.now(UTC)
        task = Task(
            id=uuid4(), tenant_id="acme", kind="coding", spec=spec, state=TaskState.QUEUED,
            created_at=now, updated_at=now, result={"turns": 0},
        )
        if issue:
            task.result["issue"] = {"provider": "github", "number": 22, "url": f"{_REPO[:-4]}/issues/22"}
        store = MemoryStore(task)
        remote = FakeForge(store)
        settings = Settings(_env_file=None, data_dir=tmp_path, scm_portal_url="https://sca.test/portal")
        secrets = SimpleNamespace(repo_credential=AsyncMock(return_value=RepoCredential(
            username="x-access-token", password=token_urlsafe(),
        )))
        service = scm_service.ScmService(settings, secrets, transport=httpx.MockTransport(remote.handle))
        monkeypatch.setattr(scm_service, "ScmService", lambda *_: service)
        deps = KindDeps(settings, store, None, FakeBackend(), None, secrets=secrets)
        state = _kernel_activities(deps)
        coding = CodingActivities(**vars(deps))
        assert coding.sync_scm in coding.all()
        task_workflow = CodingTaskWorkflow()
        decisions = iter(decisions)
        before_reviews = []
        phases = []

        async def execute(name, arg=None, **kwargs):
            if name == "load_task":
                return workflow_spec(spec)
            if name == "set_state":
                return await state.set_state(arg)
            if name == "check_budget":
                return budget_reason
            if name == "run_agent_turn":
                return TurnResult(None, "Done")
            if name == "coding.checkpoint_workspace":
                return CheckpointResult(f"commit-{arg.turn}", True)
            if name == "coding.run_verification":
                passed = auto_finish or arg.turn > 1
                return VerifyResult(passed, 0 if passed else 1, "")
            if name == "coding.publish":
                task.result.update(
                    turns=arg.turn, branch="sca/12345678", commit=f"commit-{arg.turn}",
                    pushed=True, verification_passed=arg.verification_passed,
                )
                task.usage["cost_micro_usd"] = arg.turn * 50000
            if name == "coding.sync_scm":
                phases.append(arg.phase)
                await coding.sync_scm(arg)
            if name in {"coding.initial_prompt", "coding.feedback_prompt"}:
                return "Repair"
            return None

        async def wait(predicate, **kwargs):
            before_reviews.append(bool(remote.pull))
            decision = next(decisions)
            if decision == "cancel":
                raise asyncio.CancelledError()
            await task_workflow.submit_review(decision, "Please update" if decision == "request_changes" else None)
            assert predicate()

        monkeypatch.setattr(workflow, "execute_activity", execute)
        monkeypatch.setattr(workflow, "patched", lambda _: True)
        monkeypatch.setattr(workflow, "wait_condition", wait)
        return SimpleNamespace(
            run=lambda: task_workflow.run(str(task.id)), store=store, task=task, remote=remote,
            phases=phases, before_reviews=before_reviews, service=service, secrets=secrets, coding=coding,
        )

    return make


async def test_fix_turn_updates_pull_request_and_merges_reviewed_commit(make_workflow):
    run = make_workflow(pr={"merge_on_approve": True, "body": "Additional context", "base": "release"},
                        decisions=["request_changes", "approve"])
    assert await run.run() == "SUCCEEDED"
    assert run.phases == ["review", "review", "approve", "terminal"]
    assert run.before_reviews == [True, True]
    assert run.remote.opened == [TaskState.AWAITING_REVIEW]
    assert len(run.remote.edits) == 1 and run.remote.pull["base"]["ref"] == "release"
    body = run.remote.edits[0]
    assert "Additional context" in body and "Repair the calculation" in body
    assert "命令检查 1：通过" in body and "$0.100000 USD" in body
    assert f"https://sca.test/portal#/tasks/{run.task.id}" in body
    assert run.remote.merges == [{"sha": "commit-2"}]
    assert run.task.result["pr"] == {"provider": "github", "number": 41, "url": _PR_URL,
                                     "state": "merged"}
    assert run.store.events["pr.opened"][0] == "pr.opened"
    assert len(run.store.events) == 1
    assert "审核通过" in run.remote.comments["issues/41/comments"][0]["body"]
    outcome = run.remote.comments["issues/22/comments"][0]
    assert outcome["task_state"] == TaskState.SUCCEEDED and _PR_URL in outcome["body"]
    run.secrets.repo_credential.assert_awaited_with("acme", _REPO)


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_delayed_open_is_only_on_approval(make_workflow, decision):
    run = make_workflow(pr={"open_on": "approve"}, decisions=[decision])
    await run.run()
    assert run.before_reviews == [False]
    assert len(run.remote.opened) == (decision == "approve")
    if decision == "approve":
        assert "审核通过" in run.remote.comments["issues/41/comments"][0]["body"]
    else:
        assert run.task.state == TaskState.CANCELLED
        assert "issues/41/comments" not in run.remote.comments
    assert len(run.remote.comments["issues/22/comments"]) == 1


@pytest.mark.parametrize("decision", ["reject", "cancel"])
async def test_reject_comments_but_cancel_only_reports_issue_outcome(make_workflow, decision):
    run = make_workflow(pr={}, decisions=[decision])
    if decision == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await run.run()
    else:
        await run.run()
    assert run.task.state == TaskState.CANCELLED
    assert run.remote.merges == []
    if decision == "reject":
        assert "审核拒绝" in run.remote.comments["issues/41/comments"][0]["body"]
    else:
        assert "issues/41/comments" not in run.remote.comments
    assert "CANCELLED" in run.remote.comments["issues/22/comments"][0]["body"]


async def test_issue_outcome_when_task_fails_before_push(make_workflow):
    run = make_workflow(pr={}, budget_reason="budget limit")
    with pytest.raises(ApplicationError, match="budget limit"):
        await run.run()
    assert run.phases == ["terminal"]
    assert run.task.state == TaskState.FAILED
    assert "FAILED" in run.remote.comments["issues/22/comments"][0]["body"]
    assert run.remote.opened == []


async def test_plain_task_uses_no_scm_hooks(make_workflow):
    run = make_workflow(decisions=["approve"], issue=False)
    assert await run.run() == "SUCCEEDED"
    assert run.phases == []
    run.secrets.repo_credential.assert_not_awaited()


async def test_auto_finish_opens_at_review_without_human_comment(make_workflow):
    run = make_workflow(pr={}, auto_finish=True, issue=False)
    assert await run.run() == "SUCCEEDED"
    assert run.remote.opened == [TaskState.AWAITING_REVIEW]
    assert run.remote.comments == {}


async def test_merge_conflict_records_provider_error_and_retains_human_decision(make_workflow):
    run = make_workflow(pr={"merge_on_approve": True}, decisions=["approve"])
    run.remote.merge_status = 409
    assert await run.run() == "SUCCEEDED"
    assert run.task.result["pr"]["state"] == "open"
    assert run.store.events["scm.approve.1.failed"] == (
        "scm.failed", {"phase": "approve", "message": "github API 返回 HTTP 409"},
    )


async def test_recovers_opened_pr_after_lost_response_and_refreshes_state(make_workflow):
    run = make_workflow(pr={})
    run.task.result.update(pushed=True, branch="sca/123", turns=1, verification_passed=True)
    run.task.state = TaskState.AWAITING_REVIEW
    run.remote.fail_after_create = True
    with pytest.raises(ScmError):
        await run.service.open_for_review(run.store, await run.store.get_task(run.task.id))
    await run.service.open_for_review(run.store, await run.store.get_task(run.task.id))
    assert len(run.remote.opened) == 1
    run.remote.pull["state"] = "closed"
    refreshed = await run.service.refresh_pr(run.store, await run.store.get_task(run.task.id))
    assert refreshed.state == "closed" and run.task.result["pr"]["state"] == "closed"


async def test_publish_pushes_empty_fix_that_reverts_an_existing_pr(make_workflow, monkeypatch):
    run = make_workflow(pr={})
    run.task.result.update(
        base_sha="base", branch="sca/12345678", commit="fix",
        pr={"provider": "github", "number": 41, "url": _PR_URL, "state": "open"},
    )
    ws = SimpleNamespace(diff=AsyncMock(return_value=""), push=AsyncMock())
    monkeypatch.setattr(run.coding, "_workspace", lambda _: ws)
    monkeypatch.setattr(run.coding, "_ensure_state", AsyncMock())
    monkeypatch.setattr(run.coding, "_publish_artifact", AsyncMock())
    run.store.update_result = AsyncMock()

    await run.coding.publish(PublishInput(str(run.task.id), turn=2, verification_passed=True))

    ws.push.assert_awaited_once()
    assert ws.push.await_args.args[0] == _REPO
    assert run.store.update_result.await_args.args[1]["pushed"]
