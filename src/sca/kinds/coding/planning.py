"""Disposable planning turns and validation of untrusted plans."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import shutil
from collections.abc import Iterable
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from temporalio import activity

from sca.codegraph.index import CodeIndex, analyze_impact
from sca.codegraph.repository import index_commit
from sca.core.errors import WorkspaceError
from sca.core.models import EventType
from sca.kinds.coding.activities import CodingActivities, TurnInput
from sca.kinds.coding.prompts import planning_prompt
from sca.kinds.coding.spec import SubtaskSpec, path_scope, subtask_order, validate_shared_paths
from sca.kinds.coding.subtask_inputs import PlanningInput
from sca.kinds.coding.subtasks import ChildActivities, SubtaskActivities
from sca.orchestration.provider_errors import provider_account_error
from sca.storage.local import key_for

MAX_PLAN_BYTES = 64 * 1024
_PLAN_DEPENDENCY_GUIDANCE = (
    "A split may include ordered work. Each subtask may list prerequisite subtask names in "
    "depends_on. Keep this graph acyclic. Declare any shared files in top-level shared_paths "
    "(exact repository paths). Those files may be edited by any subtask and are the exception "
    "to the overlap rule. All other edits must fall within that subtask's paths."
)


class RejectedPlan(ValueError):
    """A platform-generated reason safe to include in an event."""


class PlannedSubtask(SubtaskSpec):
    paths: list[str] = Field(min_length=1, max_length=128)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    split: bool
    reason: str = Field(min_length=1)
    subtasks: list[PlannedSubtask] = Field(default_factory=list, max_length=8)
    shared_paths: list[str] = Field(default_factory=list, max_length=64)

    _validate_shared_paths = field_validator("shared_paths")(validate_shared_paths)

    @model_validator(mode="after")
    def validate_split(self) -> Plan:
        names = [s.name for s in self.subtasks]
        if self.split and (len(names) < 2 or len(set(names)) != len(names)):
            raise ValueError("split requires 2..8 uniquely named subtasks")
        if not self.split and (self.subtasks or self.shared_paths):
            raise ValueError("split=false requires no subtasks or shared paths")
        subtask_order({s.name: s.depends_on for s in self.subtasks})
        return self


def validate_plan(data: bytes, files: Iterable[str]) -> Plan:
    if len(data) > MAX_PLAN_BYTES:
        raise RejectedPlan("plan exceeds 64 KiB")
    plan = Plan.model_validate_json(data)
    specs = [path_scope(sub.paths) for sub in plan.subtasks]
    shared = set(plan.shared_paths)
    for file in files:
        if file not in shared and sum(spec.match_file(file, separators=()) for spec in specs) > 1:
            raise RejectedPlan(f"overlapping paths: {file}")
    return plan


def planner_context(index: CodeIndex, instruction: str) -> str:
    mentions = {token.removeprefix("./").rstrip(".:")
                for token in re.findall(r"[\w./:-]+", instruction)}
    paths = {path for path in index.files
             if path in mentions or PurePosixPath(path).name in mentions}
    impact = analyze_impact(index, changed_paths=paths, changed_symbols=mentions)
    ranked = dict.fromkeys([
        *impact.changed_paths, *impact.impacted_files, *index.symbols, *index.files,
    ])
    lines = [
        "Repository map (tracked paths, local imports and exported symbols)",
        f"{len(index.files)} files, {len(index.test_sources)} recognized tests.",
    ]
    if impact.impacted_files:
        lines.extend([
            "Potentially affected by paths or symbols mentioned in the request: "
            + json.dumps(impact.impacted_files[:16], ensure_ascii=False),
            "Candidate tests: " + json.dumps(impact.tests_to_run[:12], ensure_ascii=False),
        ])
    for path in list(ranked)[:28]:
        table = index.symbols.get(path)
        row = [path, index.dependencies.get(path, [])[:4], table.defined[:5] if table else []]
        lines.append("- " + json.dumps(row, ensure_ascii=False)[:300])
    return "\n".join(lines)[:8000]


class PlanningActivities:
    def __init__(self, acts: CodingActivities) -> None:
        self.acts = acts

    def all(self) -> list:
        subtasks = SubtaskActivities(self.acts)
        return [self.plan_task, self.reject_plan, subtasks.prepare_base, subtasks.prepare]

    @activity.defn(name="coding.reject_plan")
    async def reject_plan(self, inp: PlanningInput) -> None:
        await self.acts.store.append_event(
            inp.task_id,
            EventType.PLAN_REJECTED,
            {"reason": inp.feedback or "planning failed", "attempt": inp.attempt},
            dedup_key=f"plan{inp.attempt}.rejected",
        )

    @activity.defn(name="coding.plan_task")
    async def plan_task(self, inp: PlanningInput) -> dict | None:
        acts = self.acts
        await acts.store.append_event(
            inp.task_id,
            EventType.PLAN_STARTED,
            {"attempt": inp.attempt, **acts._activity_route()},
            dedup_key=f"plan{inp.attempt}.started",
        )
        child = ChildActivities(acts, f"plan-{inp.attempt}")
        sandbox_id = None
        try:
            await acts.prepare_mirror(inp.task_id)
            task = await acts.store.get_task(inp.task_id)
            ws = acts._workspace(inp.task_id)
            base = task.result["base_sha"]
            files = await ws._git(
                "--git-dir",
                str(ws.mirror),
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                base,
            )
            spec = await acts._spec(inp.task_id)
            context = ""
            if ws.mirror.is_dir():
                with contextlib.suppress(OSError, WorkspaceError):
                    index = await index_commit(
                        ws.mirror, base, acts.settings.data_dir / "layers", task.tenant_id
                    )
                    context = planner_context(index, spec.instruction)
            child_ws = child._workspace(inp.task_id)
            await child_ws.prepare(str(ws.mirror), base)
            await child.store.update_result(
                inp.task_id,
                {
                    "base_sha": base,
                    "branch": child_ws.branch,
                },
            )
            sandbox_id = await child.create_sandbox(inp.task_id)
            await child.run_agent_turn(
                TurnInput(
                    inp.task_id,
                    0,
                    planning_prompt(spec.instruction, inp.feedback, spec.prompt_version)
                    + "\n" + "\n".join([
                        _PLAN_DEPENDENCY_GUIDANCE,
                        context,
                        *task.result.get("intents", {}).get("notes", []),
                        task.result.get("memory_note", ""),
                    ]),
                )
            )
            exit_event, output = await child._run(
                sandbox_id,
                ["bash", "-lc", "set -o pipefail; head -c 65537 .sca/plan.json | base64 | tr -d '\\n'"],
                timeout_s=30,
            )
            if exit_event.exit_code:
                raise RejectedPlan("plan file unreadable")
            data = base64.b64decode(output, validate=True)
            plan = validate_plan(data, files.rstrip("\0").split("\0"))
            result = plan.model_dump(exclude_unset=True)
            result.setdefault("subtasks", [])
            encoded = json.dumps(result, ensure_ascii=False).encode()
            uri = await acts.objects.put(
                key_for(inp.task_id, "plan.json"), encoded, "application/json"
            )
            await acts.store.add_artifact(
                inp.task_id,
                "plan.json",
                "application/json",
                uri,
                len(encoded),
            )
            await acts.store.append_event(
                inp.task_id,
                EventType.PLAN_COMPLETED,
                {**result, "attempt": inp.attempt},
                dedup_key=f"plan{inp.attempt}.completed",
            )
            if plan.split and spec.plan_review:
                await acts.store.append_event(
                    inp.task_id,
                    EventType.REVIEW_REQUESTED,
                    {"plan": result, "artifact": "plan.json", "attempt": inp.attempt},
                    dedup_key=f"plan{inp.attempt}.review_requested",
                )
            return result
        except (RejectedPlan, ValidationError) as exc:
            reason = (
                "invalid plan: " + ", ".join(e["type"] for e in exc.errors(include_input=False))
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            await self.reject_plan(PlanningInput(inp.task_id, inp.attempt, reason))
            return None
        except Exception as exc:
            if provider_account_error(exc):
                raise
            # Backend and sandbox failures may contain credentials or arbitrary output.
            await self.reject_plan(
                PlanningInput(
                    inp.task_id,
                    inp.attempt,
                    "planning turn or plan validation failed",
                )
            )
            return None
        finally:
            if sandbox_id is None:
                with contextlib.suppress(Exception):
                    sandbox_id = await child._sandbox_id(inp.task_id)
            if sandbox_id:
                with contextlib.suppress(Exception):
                    await asyncio.shield(acts.sandbox.destroy(sandbox_id))
            await asyncio.to_thread(shutil.rmtree, child._task_dir(inp.task_id), True)
