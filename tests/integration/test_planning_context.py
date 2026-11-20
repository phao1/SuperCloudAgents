"""Task context propagation without external services."""

import io
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sca.kernel.activities import KernelActivities, StepInput, VerifyResult
from sca.kernel.workflow import TaskWorkflow
from sca.kinds.coding.subtasks import ChildActivities, ChildStore
from sca.kinds.coding.workflow import CodingSubtaskWorkflow


@pytest.mark.parametrize("early", [True, False])
@pytest.mark.parametrize("split", [True, False])
async def test_task_context_order_and_unsplit_prompt(monkeypatch, early, split):
    from sca.kernel import workflow as module

    events = []
    prompts = []
    spec = {
        "max_fix_attempts": 0, "turn_timeout_s": 10, "memory_enabled": True,
        "intents": [{"path": "rules.toml"}], "auto_finish": True,
        "hooks": {"prepare": "prepare", "extensions": ["extension"] if split else [],
                  "initial_prompt": "prompt", "checkpoint": "checkpoint", "verify": "verify", "publish": "publish"},
    }

    async def execute(name, arg=None, **kwargs):
        events.append(name)
        if name == "load_task":
            return spec
        if name == "memory_initial_prompt":
            events.append("memory.injected")
            return "memory note"
        if name == "compile_intents":
            return {"notes": ["rule note"]}
        if name == "prompt":
            return "implement"
        if name == "run_agent_turn":
            prompts.append(arg.prompt)
            return SimpleNamespace(session_id="session")
        if name in {"verify", "verify_intents"}:
            return VerifyResult(True, 0, "")

    async def extension(*args):
        events.append("plan.started")
        if early:
            assert "memory note" in spec["_context_notes"]
            assert "rule note" in spec["_context_notes"]
        return {"skip_turn": True}

    monkeypatch.setattr(module.workflow, "execute_activity", execute)
    monkeypatch.setattr(module.workflow, "patched", lambda name: early)
    runner = TaskWorkflow()
    monkeypatch.setattr(runner, "_extension", extension)
    assert await runner._run("task") == "SUCCEEDED"
    assert events.count("memory.injected") == 1
    if split:
        assert (events.index("memory.injected") < events.index("plan.started")) == early
    else:
        assert "memory note" in prompts[0] and "rule note" in prompts[0]


@pytest.mark.parametrize("name", ["one", "two"])
async def test_subtask_prompts_and_collection_before_cleanup(monkeypatch, name):
    from sca.kinds.coding import workflow as module

    actions = []

    async def execute(activity, arg=None, **kwargs):
        if activity == "coding.subtask_prompt":
            return "implement"
        actions.append(arg.action)
        if arg.action == "run_agent_turn":
            assert "memory note" in arg.prompt and "rule note" in arg.prompt
            return {"session_id": "session"}
        if arg.action == "checkpoint_workspace":
            return {"commit": "sha", "changed": True}
        if arg.action == "run_verification":
            return {"passed": True}
        return {}

    monkeypatch.setattr(module.workflow, "execute_activity", execute)
    await CodingSubtaskWorkflow().run("task", {"name": name}, {
        "turn_timeout_s": 10, "max_fix_attempts": 0, "memory_enabled": True,
        "_context_notes": "memory note\nrule note",
    })
    assert actions[-2:] == ["collect_memory_proposals", "destroy_sandbox"]


async def test_child_context_and_proposals_use_parent_store(tmp_path):
    result = {"memory_context": "integer cents", "memory_note": "read MEMORY.md",
              "intents": {"notes": ["rules"]}, "subtask.one.base_sha": "base"}
    parent = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(
            result=result, model_copy=lambda update: SimpleNamespace(**update))),
    )
    scoped = ChildStore(parent, "one")
    task = await scoped.get_task("task")
    assert task.result["memory_context"] == "integer cents"
    assert task.result["intents"] == result["intents"]
    assert task.result["base_sha"] == "base"

    proposals = []
    tenant = SimpleNamespace(propose_memories=AsyncMock(side_effect=lambda *args: proposals.append(args)))
    scoped_task = SimpleNamespace(tenant_id="tenant", result={})
    store = SimpleNamespace(get_task=AsyncMock(return_value=scoped_task), for_tenant=lambda _: tenant)
    archive = io.BytesIO()
    content = b"- [pitfall] Keep integer cents\n"
    with tarfile.open(fileobj=archive, mode="w") as tar:
        member = tarfile.TarInfo("memory-proposals.md")
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))

    async def download(*args):
        yield archive.getvalue()

    child = object.__new__(ChildActivities)
    child.name = "one"
    child.store = store
    child.sandbox = SimpleNamespace(download=download)
    child._sandbox_id = AsyncMock(return_value="sandbox")
    await KernelActivities.collect_memory_proposals(child, StepInput("task", 0))
    assert proposals[0][0] == "task"
    assert proposals[0][2] == [{"text": "Keep integer cents", "category": "pitfall"}]
    assert proposals[0][3] == "subtask.one.0"


@pytest.mark.parametrize("name", ["plan-1", "one", "two"])
async def test_each_child_workspace_restores_memory_and_rules(tmp_path, name):
    from sca.config import Settings
    from sca.core.models import SandboxAdditions

    task_id = "00000000-0000-0000-0000-000000000001"
    task = SimpleNamespace(kind="coding", tenant_id="tenant", spec={}, result={
        "memory_context": "amounts are integer cents",
        "intents": {"context_keys": {"rules.toml": "rules"}},
    })
    task.model_copy = lambda update: SimpleNamespace(**{**vars(task), **update})
    child = object.__new__(ChildActivities)
    child.name = name
    child.settings = Settings(data_dir=tmp_path, pip_cache=False, state_transport="mount")
    child.store = ChildStore(SimpleNamespace(
        get_task=AsyncMock(return_value=task), update_result=AsyncMock()), name)
    child.objects = SimpleNamespace(get=AsyncMock(return_value=b"line-length = 100"))
    child.scenarios = {}
    child._ensure_state = AsyncMock()
    child._spec = AsyncMock(return_value=SimpleNamespace(image="image", model=None, agent_version=None))
    child._backend = lambda spec: SimpleNamespace(sandbox_additions=lambda ctx: SandboxAdditions())
    child.sandbox = SimpleNamespace(create=AsyncMock(return_value="sandbox"))

    async def push(sandbox, directory):
        workspace = child._workspace(task_id).workspace
        assert (workspace / ".sca/context/MEMORY.md").read_text() == "amounts are integer cents"
        assert (workspace / ".sca/context/rules.toml").read_bytes() == b"line-length = 100"

    child.transport = SimpleNamespace(mounts=lambda directory: [], push=push)
    child._workspace(task_id).workspace.mkdir(parents=True)
    await child._create_sandbox(task_id)


async def test_memory_retrieval_without_workspace_is_cached(tmp_path):
    result = {}
    task = SimpleNamespace(tenant_id="tenant", result=result)

    async def update(task_id, patch):
        result.update(patch)

    async def inject(*args, **kwargs):
        result["memory_context"] = "integer cents"
        return result["memory_context"]

    tenant = SimpleNamespace(memory_query=AsyncMock(return_value=None), inject_memories=AsyncMock(side_effect=inject))
    acts = object.__new__(KernelActivities)
    acts.settings = SimpleNamespace(memory_max_items=30, memory_max_chars=3000)
    acts.gateway = None
    acts.store = SimpleNamespace(get_task=AsyncMock(return_value=task),
                                 update_result=update, for_tenant=lambda _: tenant)
    acts._workspace = lambda _: SimpleNamespace(workspace=tmp_path / "not-prepared")
    note = await acts.memory_initial_prompt("task")
    assert await acts.memory_initial_prompt("task") == note
    assert "MEMORY.md" in note and result["memory_context"] == "integer cents"
    assert result["memory_note"] == note
    tenant.inject_memories.assert_awaited_once()
    tenant.memory_query.assert_awaited_once()
    assert not (tmp_path / "not-prepared").exists()


async def test_checkpoint_excludes_explicitly_staged_context(tmp_path):
    import asyncio

    from sca.core.models import ExecExit
    from sca.kinds.coding.activities import CodingActivities
    from sca.workspace.git import GitWorkspace

    ws = GitWorkspace(tmp_path, "main")
    await ws._git("init", "-b", "main")
    (tmp_path / "code.py").write_text("pass\n")
    (tmp_path / ".sca/context").mkdir(parents=True)
    (tmp_path / ".sca/context/MEMORY.md").write_text("private convention")
    (tmp_path / ".sca/memory-proposals.md").write_text("- proposal")
    await ws._git("add", "-f", ".sca")
    acts = object.__new__(CodingActivities)
    acts._ensure_state = AsyncMock()
    acts.store = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(result={"base_sha": "base"})),
        update_result=AsyncMock(), append_event=AsyncMock(),
    )
    acts._sandbox_id = AsyncMock(return_value="fake")
    acts._workspace = lambda _: ws
    acts._task_dir = lambda _: tmp_path
    acts._save_state = AsyncMock()
    ws.changed = AsyncMock(return_value=True)

    async def run(sandbox, argv, timeout_s):
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=tmp_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await process.communicate()
        return ExecExit(exit_code=process.returncode), (out + err).decode()

    acts._run = run
    acts.transport = SimpleNamespace(pull_commit=AsyncMock(return_value="commit"),
                                     pull_agent_home=AsyncMock())
    await acts.checkpoint_workspace(StepInput("task", 1))
    assert (await ws._git("ls-tree", "-r", "--name-only", "HEAD")).splitlines() == ["code.py"]
    assert (tmp_path / ".sca/memory-proposals.md").read_text() == "- proposal"


@pytest.mark.integration
async def test_postgres_child_proposals_accumulate_on_parent(store):
    tenant = store.for_tenant("context-test")
    task, _ = await tenant.create_task(
        tenant_id="context-test", kind="coding", spec={"repo_url": "https://example.com/repo"},
        idempotency_key=None,
    )
    archive = io.BytesIO()
    content = b"- [pitfall] Keep integer cents\n"
    with tarfile.open(fileobj=archive, mode="w") as tar:
        member = tarfile.TarInfo("memory-proposals.md")
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))

    async def download(*args):
        yield archive.getvalue()

    for name in ("one", "two", "one"):
        child = object.__new__(ChildActivities)
        child.name = name
        child.store = ChildStore(store, name)
        child.sandbox = SimpleNamespace(download=download)
        child._sandbox_id = AsyncMock(return_value="fake")
        await child.collect_memory_proposals(StepInput(str(task.id), 0))
    result = (await tenant.get_task(task.id)).result
    assert len(result["memory_proposals"]) == 1
    assert result["memory_agent_proposal:subtask.one.0"] == result["memory_proposals"]
    assert result["memory_agent_proposal:subtask.two.0"] == result["memory_proposals"]
