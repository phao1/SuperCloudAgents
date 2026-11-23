import asyncio
import json
import shlex
import sys
from dataclasses import asdict
from types import SimpleNamespace

import httpx
import pytest
from temporalio import workflow
from test_best_of import LocalWorkflow

from sca.agents.fake import FakeBackend
from sca.api import conversation
from sca.api.authz import GuestPrincipal
from sca.config import Settings
from sca.core.models import Principal, TaskState
from sca.core.task_dag import build_task_dag
from sca.kernel.activities import KernelActivities
from sca.kernel.kind import KindDeps
from sca.kinds.coding.kind import CodingKind
from sca.kinds.coding.spec import CodingSpec
from sca.kinds.coding.workflow import CodingBeforeTurn
from sca.sandbox.local import LocalSandboxProvider
from sca.storage.local import LocalObjectStore, key_for
from sca.workspace.output import prepare_output
from scripts.dag_demo import FILES, GRAPH, run_demo, submission
from tests.best_of_harness import TASK_ID, FakeStore

# Git Bash, native Git, and the Job Object launcher add subprocess startup time on Windows.
DEMO_TIMEOUT_S = 90 if sys.platform == "win32" else 20


def test_demo_submits_queues_guidance_and_waits_for_second_review():
    path = f"/v1/tasks/{TASK_ID}"
    requests = []
    sent = []
    dag_reads = 0

    def handler(request):
        nonlocal dag_reads
        method, target = request.method, request.url.path
        requests.append((method, target))
        if target == "/v1/tasks":
            spec = json.loads(request.content)["spec"]
            assert spec["agent_backend"] == "fake"
            assert spec["plan"] == "auto"
            assert spec["repo_url"] == "/fixture.git"
            assert spec["base_ref"] == "fixture"
            assert spec["max_fix_attempts"] == 1
            return httpx.Response(201, json={"id": str(TASK_ID)})
        if target == path:
            approved = ("POST", path + "/review") in requests
            return httpx.Response(200, json={
                "state": "SUCCEEDED" if approved else "AWAITING_REVIEW",
                "result": {"verification_passed": True},
            })
        if target == path + "/messages":
            assert json.loads(request.content)["mode"] == "queue"
            return httpx.Response(201, json={"id": "m1"})
        if target == path + "/dag":
            dag_reads += 1
            return httpx.Response(200, json={"nodes": [
                {"kind": "interjection", "status": "succeeded", "meta": {"message_id": "m1", "turn": 2}},
                {"kind": "review", "status": "running", "meta": {"turn": min(2, dag_reads)}},
            ]})
        assert target == path + "/review" and method == "POST" and dag_reads == 2
        assert json.loads(request.content) == {"decision": "approve"}
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://fake") as client:
        assert run_demo(client, "/fixture.git", "fixture", interval_s=0, on_submitted=sent.append) == str(TASK_ID)
    assert sent == [str(TASK_ID)]
    assert requests[-1] == ("GET", path)


class DemoStore(FakeStore):
    pending = None

    async def set_state(self, task_id, state, reason=None, *, wait_for_messages=False,
                        write_output=None):
        if wait_for_messages and self.pending:
            return await self.get_task(task_id)
        if write_output:
            assert state == TaskState.SUCCEEDED
            result = await write_output(self.task)
            self.task.result.update(result)
            await self.append_event(task_id, "output.written", result, dedup_key="output.written")
        return await super().set_state(task_id, state, reason, wait_for_messages=wait_for_messages)

    async def pending_task_messages(self, task_id):
        return [] if self.pending is None else [SimpleNamespace(id="m1", text=self.pending)]

    async def start_guided_turn(self, task_id, start, key, message_ids, interrupted_ids, *, plan_attempt=None):
        await super().start_guided_turn(task_id, start, key, message_ids, interrupted_ids, plan_attempt=plan_attempt)
        if message_ids:
            assert plan_attempt is None and message_ids == ["m1"]
            self.delivered_turn = start["turn"]
            await self.append_event(task_id, "user.message.delivered", {
                "message_id": "m1", "turn": self.delivered_turn,
            })

    async def apply_task_messages(self, task_id, message_ids):
        assert message_ids == ["m1"]
        self.pending = None
        await self.append_event(task_id, "user.message.applied", {
            "message_id": "m1", "turn": self.delivered_turn,
        })

    async def task_conversation_events(self, task_id, after, limit, types, context):
        events = await self.list_events(task_id, limit=10_000)
        return self.task, [e for e in events if e.seq > after and e.type in types][:limit], [
            e for e in events if e.type in context
        ]


class DemoWorkflow(LocalWorkflow):
    def __init__(self, tmp_path, spec, monkeypatch, *, transport="mount"):
        # Match a tenant with a cost cap on a worker without a model gateway.
        self.store = DemoStore(CodingSpec.model_validate({**spec, "max_cost_usd": 1}).model_dump())
        self.sandboxes = tmp_path / "local-sandboxes"
        self.objects = LocalObjectStore(tmp_path / "objects")
        settings = Settings(_env_file=None, data_dir=tmp_path / "data", sandbox_driver="local",
                            state_transport=transport, repo_cache=False, pip_cache=False)
        agent = FakeBackend()
        agent.state_transport = settings.state_transport
        deps = KindDeps(settings, self.store, LocalSandboxProvider(self.sandboxes), agent, self.objects)
        coding_activities = CodingKind().activities(deps)
        self.coding = coding_activities[0].__self__
        kernel = KernelActivities(**{f: getattr(deps, f) for f in deps.__dataclass_fields__})
        kernel.scenarios = {"coding": self.coding}
        super().__init__(monkeypatch, SimpleNamespace(activities=kernel.kernel_all() + coding_activities))
        monkeypatch.setattr(workflow, "start_child_workflow", self.start_child)

    async def start_child(self, run, *, args, **kwargs):
        if run != "coding.before_turn":
            return asyncio.create_task(self.execute_child(run, args=args, **kwargs))
        planner = CodingBeforeTurn()

        async def signal(name, args):
            assert name == "submit_review"
            await planner.submit_review(*args)
            self.signal.set()

        execution = asyncio.create_task(planner.run(*args))
        execution.signal = signal
        return execution

    async def interject(self):
        self.store.pending = "Record the operator acknowledgement before final approval."
        await self.store.append_event(TASK_ID, "user.message", {"message_id": "m1", "text": self.store.pending})
        await self.task.user_message("m1", "queue")
        self.signal.set()

    def events(self, event_type):
        return [event for name, event in self.store.events if name == event_type]


async def test_demo_runs_real_planning_scoped_candidates_fix_and_merge(tmp_path, best_of_repo, monkeypatch):
    demo = DemoWorkflow(tmp_path, submission(str(best_of_repo.task_dir))["spec"], monkeypatch)
    ready = demo.store.states[TaskState.AWAITING_REVIEW]
    execution = asyncio.create_task(demo.run(timeout_s=DEMO_TIMEOUT_S))

    async def wait_for_review():
        review = asyncio.create_task(ready.wait())
        try:
            done, _ = await asyncio.wait((execution, review), return_when=asyncio.FIRST_COMPLETED)
            if execution in done:
                pytest.fail(f"Demo ended in {execution.result()} before review")
            ready.clear()
        finally:
            review.cancel()
            await asyncio.gather(review, return_exceptions=True)

    try:
        await wait_for_review()
        assert demo.store.task.result["verification_passed"] is True
        await demo.interject()
        await wait_for_review()
        assert demo.store.task.result["verification_passed"] is True
        await demo.store.append_event(TASK_ID, "review.submitted", {"decision": "approve"})
        await demo.submit_review(str(TASK_ID), "approve", None)
        assert await execution == "SUCCEEDED"
    finally:
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)

    assert {row["name"]: row["status"] for row in demo.store.task.result["subtasks"]} == dict.fromkeys(GRAPH, "merged")
    root = demo.coding._workspace(str(TASK_ID)).workspace
    assert {path: (root / path).read_text().strip() for path in FILES.values()} == {
        filename: name for name, filename in FILES.items()
    }
    assert not (root / "dag-demo/pricing.pending").exists()
    patch = await demo.objects.get(key_for(TASK_ID, "changes.patch"))
    assert b"+++ b/dag-demo/operator.txt" in patch and b"+operator guidance applied" in patch
    assert len(demo.events("best_of.attempt_completed")) == 3
    selected, = demo.events("best_of.selected")
    assert (selected["subtask"], selected["attempt"]) == ("fx", 2)
    verification = [e for e in demo.events("verification.completed") if e.get("subtask") == "pricing"]
    assert [e["passed"] for e in verification] == [False, True]
    events = await demo.store.list_events(TASK_ID, 1000)
    graph = build_task_dag(demo.store.task, events, [])
    assert len([node for node in graph.nodes if node.kind == "subtask"]) == 8
    assert len([node for node in graph.nodes if node.kind == "attempt" and node.status == "succeeded"]) == 3
    assert [node.meta["subtask"] for node in graph.nodes if node.kind == "fix"] == ["pricing"]
    assert {node.status for node in graph.nodes if node.kind in ("interjection", "finish")} == {"succeeded"}
    assert [node.status for node in graph.nodes if node.kind == "review"] == ["skipped", "succeeded"]
    assert not list(demo.sandboxes.iterdir())
    children = root.parent / "sub"
    assert not list(children.glob("plan-*"))
    assert not list(children.glob("*/sub/best-of-*"))


async def test_nested_candidates_cannot_adopt_an_out_of_scope_patch(tmp_path, best_of_repo, monkeypatch):
    options = submission(str(best_of_repo.task_dir))["spec"]
    options["instruction"] = options["instruction"].replace(
        "> dag-demo/fx.txt", "> dag-demo/fx.txt && printf out-of-scope > app.txt",
    )
    demo = DemoWorkflow(tmp_path, {**options, "auto_finish": True}, monkeypatch)
    assert await demo.run(timeout_s=DEMO_TIMEOUT_S) == "FAILED"
    assert len({p["best_of_attempt"] for p in demo.events("scope.violation")}) == 3
    assert demo.events("best_of.selected") == []
    root = demo.coding._workspace(str(TASK_ID)).workspace
    assert (root / "app.txt").read_text() == "broken\n"
    assert not (root / FILES["fx"]).exists()


@pytest.mark.parametrize(("mode", "auto_finish"), [
    ("none", False), ("none", True), ("plan-review", True), ("verification-fails", True),
])
async def test_output_delivery_follows_the_final_review_or_auto_finish(
    tmp_path, monkeypatch, mode, auto_finish,
):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    directory, branch, target = prepare_output(settings, str(tmp_path / "output"), "Deliver code", None)
    output = tmp_path / "output"
    plan = mode == "plan-review"
    commands = "printf 'app\n' > app.txt"
    if plan:
        subtasks = [
            {"name": "app", "instruction": "Create the app", "paths": ["app.txt"]},
            {"name": "docs", "instruction": "Write the docs", "paths": ["docs.txt"]},
        ]
        encoded = shlex.quote(json.dumps({"split": True, "reason": "Independent work", "subtasks": subtasks}))
        commands = (
            'case "$(git branch --show-current)" in\n'
            f'  *-plan-*) mkdir -p .sca && printf %s {encoded} > .sca/plan.json ;;\n'
            "  *-app) printf 'app\n' > app.txt ;;\n"
            "  *-docs) printf 'docs\n' > docs.txt ;;\n"
            "esac"
        )
    demo = DemoWorkflow(tmp_path, {
        "repo_url": directory, "output_dir": directory, "base_ref": branch,
        "instruction": f"Deliver code\n\n```sca-fake\n{commands}\n```",
        "plan": "auto" if plan else "none", "plan_review": plan,
        "agent_backend": "fake", "auto_finish": auto_finish, "memory_enabled": False,
        "max_fix_attempts": 0, "verify_command": "false" if mode == "verification-fails" else None,
    }, monkeypatch, transport="sync" if auto_finish else "mount")
    task = demo.store.task
    task.tenant_id = "eval"
    task.result["output_target"] = asdict(target)
    execution = asyncio.create_task(demo.run(timeout_s=DEMO_TIMEOUT_S))
    try:
        if not auto_finish or plan:
            await asyncio.wait_for(demo.store.states[TaskState.AWAITING_REVIEW].wait(), DEMO_TIMEOUT_S)
            assert not (output / "app.txt").exists()
            assert demo.events("output.written") == []
            await demo.store.append_event(TASK_ID, "review.submitted", {"decision": "approve"})
            await demo.submit_review(str(TASK_ID), "approve", None)
        if mode == "verification-fails":
            assert await execution == TaskState.FAILED
            assert not (output / "app.txt").exists()
            assert demo.events("output.written") == []
            return
        assert await execution == TaskState.SUCCEEDED
    finally:
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
    assert (output / "app.txt").read_text() == "app\n"
    if plan:
        assert (output / "docs.txt").read_text() == "docs\n"
        assert len(demo.events("plan.completed")) == 1
        assert len(demo.events("subtasks.merged")) == 1
    assert not (output / ".sca").exists()
    written, = demo.events("output.written")
    assert written["output_commit"] == task.result["output_commit"]
    assert written["output_branch"] == "main"
    events = await demo.store.list_events(TASK_ID, 1000)
    dag = build_task_dag(task, events, [])
    node, = [node for node in dag.nodes if node.kind == "output"]
    assert node.label == "代码已写入" and node.status == "succeeded"
    assert any(edge.source == node.id and edge.target.endswith("/finish:finish") for edge in dag.edges)
    assert node.meta["output_dir"] == directory
    for user, expected_path in [
        (Principal(tenant_id="eval", subject="fixture"), directory),
        (GuestPrincipal(tenant_id="eval"), "本机目录"),
    ]:
        chat = await conversation.task_page(demo.store, TASK_ID, user, 0, 1000)
        item, = [item for item in chat.items if item.kind == "output.written"]
        assert item.dag_node_id == node.id
        assert item.text == f"代码已写入 {expected_path}（提交 {written['output_commit'][:7]}）"
