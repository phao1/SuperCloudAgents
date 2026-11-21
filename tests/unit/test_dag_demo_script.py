import asyncio
import json
from types import SimpleNamespace

import httpx
from temporalio import workflow
from test_best_of import LocalWorkflow

from sca.agents.fake import FakeBackend
from sca.config import Settings
from sca.core.models import TaskState
from sca.core.task_dag import build_task_dag
from sca.kernel.activities import KernelActivities
from sca.kernel.kind import KindDeps
from sca.kinds.coding.kind import CodingKind
from sca.kinds.coding.spec import CodingSpec
from sca.kinds.coding.workflow import CodingBeforeTurn
from sca.sandbox.local import LocalSandboxProvider
from sca.storage.local import LocalObjectStore, key_for
from scripts.dag_demo import FILES, GRAPH, run_demo, submission
from tests.best_of_harness import TASK_ID, FakeStore


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

    async def deliver_task_messages(self, task_id, turn, *, planning):
        if self.pending is None:
            return []
        assert not planning
        text, self.pending = self.pending, None
        self.delivered_turn = turn
        await self.append_event(task_id, "user.message.delivered", {"message_id": "m1", "turn": turn})
        return [SimpleNamespace(id="m1", text=text)]

    async def apply_task_messages(self, task_id, message_ids):
        assert message_ids == ["m1"]
        await self.append_event(task_id, "user.message.applied", {
            "message_id": "m1", "turn": self.delivered_turn,
        })


class DemoWorkflow(LocalWorkflow):
    def __init__(self, tmp_path, spec, monkeypatch):
        # Match a tenant with a cost cap on a worker without a model gateway.
        self.store = DemoStore(CodingSpec.model_validate({**spec, "max_cost_usd": 1}).model_dump())
        self.sandboxes = tmp_path / "local-sandboxes"
        self.objects = LocalObjectStore(tmp_path / "objects")
        settings = Settings(_env_file=None, data_dir=tmp_path / "data", sandbox_driver="local",
                            repo_cache=False, pip_cache=False)
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
        execution = CodingBeforeTurn().run(*args) if run == "coding.before_turn" else self.execute_child(
            run, args=args, **kwargs,
        )
        return asyncio.create_task(execution)

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
    execution = asyncio.create_task(demo.run())
    try:
        async with asyncio.timeout(18):
            await ready.wait()
            ready.clear()
            assert demo.store.task.result["verification_passed"] is True
            await demo.interject()
            await ready.wait()
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


async def test_nested_candidates_cannot_adopt_an_out_of_scope_patch(tmp_path, best_of_repo, monkeypatch):
    options = submission(str(best_of_repo.task_dir))["spec"]
    options["instruction"] = options["instruction"].replace(
        "> dag-demo/fx.txt", "> dag-demo/fx.txt && printf out-of-scope > app.txt",
    )
    demo = DemoWorkflow(tmp_path, {**options, "auto_finish": True}, monkeypatch)
    assert await demo.run() == "FAILED"
    assert len({p["best_of_attempt"] for p in demo.events("scope.violation")}) == 3
    assert demo.events("best_of.selected") == []
    root = demo.coding._workspace(str(TASK_ID)).workspace
    assert (root / "app.txt").read_text() == "broken\n"
    assert not (root / FILES["fx"]).exists()
