"""Run coding child workflows with the scripted agent and local Git fixtures."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from temporalio import activity
from temporalio.testing import ActivityEnvironment
from test_codegraph import local_io, write_files  # noqa: F401

from sca.agents.fake import FakeBackend
from sca.api import app as api_app
from sca.config import Settings
from sca.core.errors import NotFoundError
from sca.core.models import Event, ExecExit, ExecOutput, Task, TaskState
from sca.kinds.coding import workflow as coding_workflow
from sca.kinds.coding.activities import CodingActivities, PublishInput
from sca.kinds.coding.kind import prompt_activities, workflow_spec
from sca.kinds.coding.planning import PlanningActivities, validate_plan
from sca.kinds.coding.spec import CodingSpec
from sca.kinds.coding.subtask_inputs import PlanningInput
from sca.kinds.coding.workflow import CodingBeforeTurn, CodingSubtaskWorkflow
from sca.sandbox.local import LocalSandboxProvider
from sca.storage.local import LocalObjectStore
from sca.workspace.git import GitWorkspace
from sca.workspace.tarsafe import extract


def script(body: str) -> str:
    return f"\n```sca-fake\nset -e\n{body}\n```\n"


class FakeStore:
    def __init__(self, spec: CodingSpec, settings: Settings):
        self.task = Task(id=uuid4(), tenant_id="acme", kind="coding", spec=spec.model_dump(),
                         state=TaskState.PROVISIONING, created_at=datetime.now(UTC),
                         updated_at=datetime.now(UTC))
        self.events = []
        self.artifacts = {}
        self.dedup = set()
        self.sessionmaker = SimpleNamespace(
            kw={"bind": SimpleNamespace(url=make_url(settings.database_url))}
        )

    async def get_task(self, task_id, tenant_id=None):
        if str(task_id) != str(self.task.id) or tenant_id not in (None, self.task.tenant_id):
            raise NotFoundError("Task not found")
        return self.task.model_copy(deep=True)

    async def get_task_champion(self, task_id: str) -> None:
        return None

    async def tenant_deleted(self, tenant_id: str) -> bool:
        return False

    async def update_result(self, task_id, patch):
        self.task.result.update(patch)

    async def append_event(self, task_id, type_, payload, dedup_key=None):
        if dedup_key and dedup_key in self.dedup:
            return None
        self.dedup.add(dedup_key)
        event = Event(task_id=self.task.id, seq=len(self.events) + 1, type=type_,
                      payload=payload, created_at=datetime.now(UTC))
        self.events.append(event)
        return event

    async def list_events(self, task_id, limit):
        return self.events[:limit]

    async def add_artifact(self, task_id, name, *args):
        self.artifacts[name] = args

    async def add_usage(self, task_id, usage):
        self.task.usage.update(usage)

    async def check_budget(self, task_id):
        return None

    def for_tenant(self, tenant_id):
        return self


class LocalSandbox:
    def __init__(self, root):
        self.root = root
        self.paths = {}

    async def create(self, name, spec):
        root = self.root / name
        root.mkdir(parents=True, exist_ok=True)
        paths = {mount.target: Path(mount.source) for mount in spec.mounts}
        for remote in ("/workspace", "/agent_home", "/tmp"):
            if remote not in paths:
                paths[remote] = root / remote.lstrip("/")
                paths[remote].mkdir(exist_ok=True)
        self.paths[name] = paths
        return name

    async def destroy(self, sandbox_id):
        self.paths.pop(sandbox_id, None)
        shutil.rmtree(self.root / sandbox_id, ignore_errors=True)

    async def exists(self, sandbox_id):
        return sandbox_id in self.paths

    def local_path(self, sandbox_id, remote):
        root, _, rest = remote.lstrip("/").partition("/")
        return self.paths[sandbox_id][f"/{root}"] / rest

    async def exec(self, sandbox_id, argv, *, workdir="/workspace", **kwargs):
        args = [
            str(self.local_path(sandbox_id, arg)) if arg == "/tmp/sca.bundle" else arg
            for arg in argv
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.local_path(sandbox_id, workdir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        for stream, data in (("stdout", out), ("stderr", err)):
            for line in data.decode(errors="replace").splitlines():
                yield ExecOutput(stream=stream, text=line)
        yield ExecExit(exit_code=proc.returncode)

    async def upload(self, sandbox_id, target, archive):
        data = io.BytesIO()
        async for chunk in archive:
            data.write(chunk)
        data.seek(0)
        extract(data, self.local_path(sandbox_id, target))

    async def download(self, sandbox_id, path):
        data = io.BytesIO()
        source = self.local_path(sandbox_id, path)
        with tarfile.open(fileobj=data, mode="w") as archive:
            archive.add(source, arcname=source.name)
        yield data.getvalue()


class Harness:
    def __init__(self, root, spec, settings, monkeypatch):
        self.settings = settings
        self.store = FakeStore(spec, settings)
        self.task_id = str(self.store.task.id)
        self.objects = LocalObjectStore(root / "objects")
        sandbox_root = root / "sandboxes"
        sandbox = (LocalSandboxProvider(sandbox_root) if settings.sandbox_driver == "local"
                   else LocalSandbox(sandbox_root))
        self.acts = CodingActivities(
            settings=settings, store=self.store, sandbox=sandbox,
            agent=FakeBackend(), objects=self.objects,
        )
        self.planning = PlanningActivities(self.acts)
        self.activities = {
            activity._Definition.must_from_callable(fn).name: fn
            for fn in self.acts.all() + self.planning.all() + prompt_activities()
        }
        self.spec = workflow_spec(spec.model_dump())
        self.started = []
        self.completed = []
        self.peak_children = 0
        self.block_until = {}
        self.launched = {}
        monkeypatch.setattr(coding_workflow.workflow, "execute_activity", self.execute)
        monkeypatch.setattr(coding_workflow.workflow, "start_child_workflow", self.start_child)
        monkeypatch.setattr(coding_workflow.workflow, "patched", lambda _: True)

    async def execute(self, name, arg=None, *, args=None, **kwargs):
        if name == "set_state":
            self.store.task.state = TaskState(arg.state)
            return None
        return await ActivityEnvironment().run(self.activities[name], *(args or (arg,)))

    async def start_child(self, run, *, args, **kwargs):
        name = args[1]["name"]
        assert set(args[1].get("depends_on", [])) <= set(self.completed)
        self.started.append(name)
        self.peak_children = max(self.peak_children, len(self.started) - len(self.completed))
        self.launched.setdefault(name, asyncio.Event()).set()

        async def child():
            if name in self.block_until:
                await self.launched.setdefault(self.block_until[name], asyncio.Event()).wait()
            result = await CodingSubtaskWorkflow().run(*args)
            self.completed.append(name)
            return result

        return asyncio.create_task(child())

    async def run(self):
        async with asyncio.timeout(30):
            return await CodingBeforeTurn().run(self.task_id, self.spec)

    def events(self, type_):
        return [event.payload for event in self.store.events if event.type == type_]


@pytest.fixture
async def seed(tmp_path):
    ws = GitWorkspace(tmp_path / "source", "main")
    ws.task_dir.mkdir()
    await ws._git("init", "-b", "main")
    write_files(ws.task_dir, {
        "left/main.py": "from common import public\n",
        "right/main.py": "from common import public\n",
        "common.py": "def public(value: int) -> int:\n    return value\n",
        "tests/test_common.py": "from common import public\n",
    })
    await ws._git("add", ".")
    await ws._git(
        "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "base",
    )
    return ws.task_dir


@pytest.fixture
def build(tmp_path, seed, monkeypatch):
    def factory(*, transport="mount", driver="docker", **kw):
        settings = Settings(_env_file=None, data_dir=tmp_path / "data", state_transport=transport,
                            sandbox_driver=driver, repo_cache=False, pip_cache=False,
                            api_tokens="owner-fixture:acme,other-fixture:other")
        spec = CodingSpec(repo_url=str(seed), **{"instruction": "edit", **kw})
        return Harness(tmp_path, spec, settings, monkeypatch)

    return factory


async def test_dag_starts_dependencies_as_soon_as_ready(build):
    h = build(subtasks=[
        {"name": "join", "depends_on": ["relay", "right"], "instruction": script(
            "test -f left/first.py\ntest -f right/first.py\n"
            "test ! -f unrelated.py\nprintf 'value = 3\\n' > left/join.py"
        )},
        {"name": "unrelated", "instruction": script("printf 'value = 4\\n' > unrelated.py")},
        {"name": "relay", "depends_on": ["left"],
         "instruction": script("test -f left/first.py")},
        {"name": "left", "instruction": script("printf 'value = 1\\n' > left/first.py")},
        {"name": "right", "instruction": script("printf 'value = 2\\n' > right/first.py")},
    ])
    h.block_until["unrelated"] = "join"
    assert await h.run() == {"skip_turn": True, "unresolved": []}
    assert h.started.index("unrelated") < h.started.index("join")
    assert h.completed.index("relay") < h.completed.index("join")
    statuses = {row["name"]: row["status"] for row in h.store.task.result["subtasks"]}
    assert statuses == {name: "unchanged" if name == "relay" else "merged" for name in h.started}
    root = h.acts._workspace(h.task_id).workspace
    assert (root / "left/join.py").read_text() == "value = 3\n"
    assert (root / "unrelated.py").exists()
    assert h.acts.sandbox.paths == {}


@pytest.mark.parametrize("conflict", [False, True])
async def test_failed_or_conflicting_dependencies_block_agent_turn(build, conflict):
    h = build(
        max_fix_attempts=0,
        verify_command=None if conflict else 'case "$(git branch --show-current)" in *-left) exit 1;; esac',
        shared_paths=["common.py"],
        subtasks=[
            {"name": "left", "paths": ["left/"], "instruction": script("echo 'a = 1' > common.py")},
            {"name": "right", "paths": ["right/"], "instruction": script("echo 'b = 2' > common.py")},
            {"name": "join", "depends_on": ["left", "right"], "paths": ["tests/"],
             "instruction": script("false")},
        ],
    )
    result = await h.run()
    assert {row["name"]: row["status"] for row in result["unresolved"]} == {
        ("right" if conflict else "left"): ("conflict" if conflict else "failed"),
        "join": "blocked",
    }
    assert "join" not in {event["subtask"] for event in h.events("agent.turn_started")}


async def test_final_merge_blocks_changes_based_on_a_conflicted_dependency(build):
    h = build(subtasks=[
        {"name": "first", "instruction": script("echo 'a = 1' > common.py")},
        {"name": "second", "instruction": script("echo 'b = 2' > common.py")},
        {"name": "dependent", "depends_on": ["second"],
         "instruction": script("grep 'b = 2' common.py\necho 'ok = True' > followup.py")},
    ])
    result = await h.run()
    assert {row["name"]: row["status"] for row in result["unresolved"]} == {
        "second": "conflict", "dependent": "blocked",
    }
    assert "dependent" in {event["subtask"] for event in h.events("agent.turn_started")}
    assert not (h.acts._workspace(h.task_id).workspace / "followup.py").exists()


@pytest.mark.parametrize("transport", ["mount", "sync"])
async def test_scope_rejects_turn_and_keeps_session_for_retry(build, transport):
    h = build(
        transport=transport, max_fix_attempts=1,
        shared_paths=["common.py"],
        subtasks=[
            {"name": "left", "paths": ["left/**", "!left/excluded.py"], "instruction":
             script("mv right/main.py left/moved.py\necho first > left/excluded.py")
             + script("test -f right/main.py\ntest ! -f left/moved.py\n"
                      "test ! -f left/excluded.py\necho 'value = public(1)' >> left/main.py\n"
                      "echo '# shared edit' >> common.py")},
            {"name": "right", "paths": ["right/**"],
             "instruction": script("echo 'value = public(2)' >> right/main.py")},
        ],
    )
    assert await h.run() == {"skip_turn": True, "unresolved": []}
    violations = h.events("scope.violation")
    assert violations == [{
        "subtask": "left", "turn": 1, "paths": ["left/excluded.py", "right/main.py"],
        "reason": "outside_scope",
    }]
    turns = [event for event in h.events("agent.turn_started") if event["subtask"] == "left"]
    assert len(turns) == 2 and "Your last turn was discarded" in turns[1]["prompt"]
    assert all("def public(value: int) -> int" in event["prompt"] for event in turns)
    verified = [event["turn"] for event in h.events("verification.completed")
                if event["subtask"] == "left"]
    assert verified == [2]
    parent = h.acts._workspace(h.task_id).workspace
    assert "public(1)" in (parent / "left/main.py").read_text()
    assert "public(2)" in (parent / "right/main.py").read_text()
    assert "# shared edit" in (parent / "common.py").read_text()
    assert not (parent / "left/moved.py").exists()
    assert not (parent / "left/excluded.py").exists()
    assert len(h.events("scope.violation")) == 1


async def test_rejected_last_turn_leaves_no_out_of_scope_patch(build):
    h = build(max_fix_attempts=0, subtasks=[
        {"name": "left", "paths": ["left/"], "instruction": script("echo 'x = 1' > common.py")},
        {"name": "right", "depends_on": ["left"], "instruction": "Continue"},
    ])
    result = await h.run()
    assert {row["name"]: row["status"] for row in result["unresolved"]} == {
        "left": "failed", "right": "blocked",
    }
    assert h.events("scope.violation")[0]["paths"] == ["common.py"]
    assert await h.objects.get(f"tasks/{h.task_id}/subtask-left.patch") == b""


@pytest.mark.parametrize("depends", [["absent"], ["one"], ["two", "two"], ["two"]])
def test_plan_and_submission_validate_dependencies(depends):
    subtasks = [
        {"name": "one", "paths": ["left/"], "instruction": "one", "depends_on": depends},
        {"name": "two", "paths": ["right/"], "instruction": "two", "depends_on": ["one"]},
    ]
    with pytest.raises(ValidationError, match="subtask dependenc"):
        CodingSpec(repo_url="repo", instruction="edit", subtasks=subtasks)
    with pytest.raises(ValidationError, match="subtask dependenc"):
        validate_plan(json.dumps({"split": True, "reason": "test", "subtasks": subtasks}).encode(),
                      ["left/one", "right/two"])


async def test_planning_activity_supplies_graph_and_accepts_shared_paths(build):
    plan = {
        "split": True, "reason": "implementation before tests", "shared_paths": ["common.py"],
        "subtasks": [
            {"name": "impl", "instruction": "work", "paths": ["left/", "common.py"]},
            {"name": "tests", "instruction": "test", "paths": ["tests/", "common.py"],
             "depends_on": ["impl"]},
        ],
    }
    h = build(driver="local", plan="auto", instruction="Change common.py." + script(
        "mkdir -p .sca\ncat > .sca/plan.json <<'PLAN'\n" + json.dumps(plan) + "\nPLAN"
    ))
    result = await ActivityEnvironment().run(h.planning.plan_task, PlanningInput(h.task_id))
    assert result == plan
    prompt = h.events("agent.turn_started")[0]["prompt"]
    assert "Repository map" in prompt and "Candidate tests" in prompt
    assert "tests/test_common.py" in prompt and "prerequisite subtask names" in prompt
    assert await h.acts.sandbox.list({}) == []


async def test_planner_and_subtasks_use_selected_dsh_backend_and_model(build):
    plan = {"split": True, "reason": "separate modules", "parallelism": 2, "subtasks": [
        {"name": name, "instruction": "inspect module", "paths": [f"{name}/main.py"]}
        for name in ("left", "right")
    ]}
    command = "\n".join([
        'if [[ "$(git branch --show-current)" == *-plan-* ]]; then',
        "mkdir -p .sca", "cat > .sca/plan.json <<'PLAN'", json.dumps(plan), "PLAN", "fi",
    ])
    h = build(driver="local", plan="auto", instruction=script(command), agent_backend="dsh",
              model="fixture-model", use_champion=False, memory_enabled=False, max_parallel_agents=1)
    models = []

    class ScriptedDsh(FakeBackend):
        name = "dsh"
        state_transport = "sync"

        async def run_turn(self, sandbox, sandbox_id, ctx, prompt, **options):
            models.append(ctx.model)
            async for event in super().run_turn(sandbox, sandbox_id, ctx, prompt, **options):
                yield event

    h.acts.backends["dsh"] = ScriptedDsh()
    h.settings.max_parallel_agents = 2
    h.spec = await ActivityEnvironment().run(h.acts.load_task, h.task_id)
    h.spec["_user_interjection"] = False  # This fixture runs activity calls without signal handles.
    assert h.spec["_max_parallel_agents"] == 1
    assert await h.run() == {"skip_turn": True, "unresolved": []}
    assert h.peak_children == 1
    assert h.events("plan.completed")[0]["max_parallel_agents"] == 1
    assert models == ["fixture-model"] * 3
    assert {event["subtask"] for event in h.events("agent.turn_started") if event["backend"] == "dsh"} == {
        "plan-1", "left", "right",
    }


async def test_impact_endpoint_uses_published_commit_and_durable_bundle(build, monkeypatch):
    h = build(subtasks=[
        {"name": "one", "paths": ["common.py"], "instruction": script("echo '# changed' >> common.py")},
        {"name": "two", "instruction": script("true")},
    ])
    assert (await h.run())["unresolved"] == []
    await ActivityEnvironment().run(h.acts.publish, PublishInput(h.task_id, 1, True))
    published = h.store.task.result["commit"]
    h.store.task.result["commit"] = h.store.task.result["base_sha"]
    monkeypatch.setattr(api_app, "get_settings", lambda: h.settings)
    app = api_app.create_app(store=h.store, object_store=h.objects)
    route = f"/v1/tasks/{h.task_id}/impact"
    owner = {"Authorization": "Bearer owner-fixture"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as http:
        assert (await http.get(route, headers={"Authorization": "Bearer other-fixture"})).status_code == 404
        response = await http.get(route, headers=owner)
        assert response.status_code == 200
        result = response.json()
        assert result["commit"] == published
        assert result["changed_paths"] == ["common.py"]
        assert result["tests_to_run"] == ["tests/test_common.py"]
        assert "left/main.py" in result["impacted_files"]
        shutil.rmtree(h.settings.task_dir(h.task_id))
        assert (await http.get(route, headers=owner)).json() == result
        h.store.task.result.pop("patch_artifact")
        assert (await http.get(route, headers=owner)).status_code == 404
