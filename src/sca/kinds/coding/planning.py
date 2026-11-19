"""Disposable planning turns and validation of untrusted plans."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import shutil
from collections.abc import Iterable

import pathspec
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from temporalio import activity

from sca.core.models import EventType
from sca.kinds.coding.activities import CodingActivities, TurnInput
from sca.kinds.coding.prompts import planning_prompt
from sca.kinds.coding.spec import SubtaskSpec
from sca.kinds.coding.subtask_inputs import PlanningInput
from sca.kinds.coding.subtasks import ChildActivities
from sca.storage.local import key_for

MAX_PLAN_BYTES = 64 * 1024


class RejectedPlan(ValueError):
    """A platform-generated reason safe to include in an event."""


class PlannedSubtask(SubtaskSpec):
    paths: list[str] = Field(min_length=1)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    split: bool
    reason: str = Field(min_length=1)
    subtasks: list[PlannedSubtask] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_split(self) -> Plan:
        names = [s.name for s in self.subtasks]
        if self.split and (len(names) < 2 or len(set(names)) != len(names)):
            raise ValueError("split requires 2..8 uniquely named subtasks")
        if not self.split and self.subtasks:
            raise ValueError("split=false requires no subtasks")
        if any(not p.strip() for s in self.subtasks for p in s.paths):
            raise ValueError("paths must not be empty")
        return self


def validate_plan(data: bytes, files: Iterable[str]) -> Plan:
    if len(data) > MAX_PLAN_BYTES:
        raise RejectedPlan("plan exceeds 64 KiB")
    plan = Plan.model_validate_json(data)
    specs = [pathspec.PathSpec.from_lines("gitwildmatch", sub.paths) for sub in plan.subtasks]
    for file in files:
        if sum(spec.match_file(file) for spec in specs) > 1:
            raise RejectedPlan(f"overlapping paths: {file}")
    return plan


class PlanningActivities:
    def __init__(self, acts: CodingActivities) -> None:
        self.acts = acts

    def all(self) -> list:
        return [self.plan_task, self.reject_plan]

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
            files = await ws._git(
                "--git-dir",
                str(ws.mirror),
                "ls-tree",
                "-r",
                "--name-only",
                "-z",
                task.result["base_sha"],
            )
            child_ws = child._workspace(inp.task_id)
            await child_ws.prepare(str(ws.mirror), task.result["base_sha"])
            await child.store.update_result(
                inp.task_id,
                {
                    "base_sha": task.result["base_sha"],
                    "branch": child_ws.branch,
                },
            )
            sandbox_id = await child.create_sandbox(inp.task_id)
            spec = await acts._spec(inp.task_id)
            await child.run_agent_turn(
                TurnInput(
                    inp.task_id,
                    0,
                    planning_prompt(spec.instruction, inp.feedback, spec.prompt_version),
                )
            )
            exit_event, output = await child._run(
                sandbox_id,
                ["bash", "-lc", "set -o pipefail; head -c 65537 .sca/plan.json | base64 -w0"],
                timeout_s=30,
            )
            if exit_event.exit_code:
                raise RejectedPlan("plan file unreadable")
            data = base64.b64decode(output, validate=True)
            plan = validate_plan(data, files.rstrip("\0").split("\0"))
            encoded = plan.model_dump_json().encode()
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
            result = plan.model_dump()
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
        except Exception:  # noqa: BLE001 — planning failures must fall back
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
