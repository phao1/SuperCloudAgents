"""Temporal activities persist the selected version on every scoped agent turn."""

import shutil
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from sca.config import Settings, get_settings
    from sca.core.models import AgentEvent, AgentEventType, EventType
    from sca.kernel import prompts
    from sca.kernel.activities import TurnInput
    from sca.kinds.coding.activities import CodingActivities
    from sca.kinds.coding.kind import ci_fix_prompt, initial_prompt, subtask_prompt
    from sca.kinds.coding.prompts import planning_prompt
    from sca.kinds.coding.spec import CodingSpec
    from sca.kinds.coding.subtasks import ChildActivities

pytestmark = pytest.mark.integration


@workflow.defn
class PromptTurnsWorkflow:
    @workflow.run
    async def run(self, task_id: str, spec: dict) -> None:
        for scope in ("planning", "subtask", "initial", "ci_fix"):
            await workflow.execute_activity(
                "test_versioned_turn", args=[task_id, spec, scope],
                start_to_close_timeout=timedelta(seconds=30),
            )


async def test_workflow_turn_events_use_task_version(store, tmp_path, monkeypatch):
    try:
        client = await Client.connect(get_settings().temporal_address)
    except (RuntimeError, OSError) as exc:
        pytest.skip(f"Temporal unavailable: {exc}")
    root = tmp_path / "coding/prompts/v-test"
    shutil.copytree(prompts.PROMPTS_ROOT / "coding/prompts/v1", root)
    for file in root.glob("*.txt"):
        file.write_text("VERSION TEST\n" + file.read_text())
    monkeypatch.setattr(prompts, "PROMPTS_ROOT", tmp_path)
    spec = CodingSpec(repo_url="x", instruction="work", prompt_version="v-test").model_dump()
    task, _ = await store.create_task(tenant_id="versions", kind="coding", spec=spec,
                                      idempotency_key=None)
    await store.update_result(task.id, {"prompt_version": "v-test", "sandbox_id": "test"})

    class Agent:
        name = "scripted"

        async def run_turn(self, *args, **kwargs):
            yield AgentEvent(type=AgentEventType.TURN_COMPLETED, data={"final_message": "done"})

    acts = CodingActivities(settings=Settings(data_dir=tmp_path), store=store,
                            sandbox=SimpleNamespace(), agent=Agent(), objects=SimpleNamespace())

    @activity.defn(name="test_versioned_turn")
    async def turn(task_id: str, spec: dict, scope: str) -> None:
        scoped = ChildActivities(acts, scope) if scope in {"planning", "subtask"} else acts
        scoped._ensure_state = AsyncMock(return_value=False)
        scoped._before_agent_turn = AsyncMock()
        scoped._sandbox_id = AsyncMock(return_value="test")
        scoped.transport = SimpleNamespace(pull_agent_home=AsyncMock())
        if scope == "planning":
            text = planning_prompt(spec["instruction"], None, spec["prompt_version"])
        elif scope == "subtask":
            text = await subtask_prompt(spec, {"name": "one", "instruction": "child"})
        elif scope == "ci_fix":
            text = await ci_fix_prompt("failed", None, spec["prompt_version"])
        else:
            text = await initial_prompt(spec)
        await scoped.run_agent_turn(TurnInput(task_id, 2 if scope == "ci_fix" else 1, text))

    queue = f"prompt-versions-{uuid4()}"
    async with Worker(client, task_queue=queue, workflows=[PromptTurnsWorkflow], activities=[turn]):
        await client.execute_workflow(PromptTurnsWorkflow.run, args=[str(task.id), spec],
                                      id=f"prompts-{uuid4()}", task_queue=queue)
    turns = [e for e in await store.list_events(task.id) if e.type == EventType.AGENT_TURN_STARTED]
    assert len(turns) == 4
    assert {e.payload.get("subtask") for e in turns} == {None, "planning", "subtask"}
    assert all(e.payload["prompt_version"] == "v-test" for e in turns)
    assert all(e.payload["prompt"].startswith("VERSION TEST\n") for e in turns)
