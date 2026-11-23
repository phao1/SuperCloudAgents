"""Prepared workspace forks and publication for Best-of-N coding tasks."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import asdict, replace
from decimal import Decimal

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sca.core.errors import InvalidTaskError, WorkspaceError
from sca.core.models import AgentContext, Event, EventType, Task
from sca.host import remove_tree
from sca.kernel.activities import StepInput, VerifyResult, _non_retryable
from sca.kernel.kind import ExecutionSpec
from sca.kinds.coding.activities import CodingActivities
from sca.kinds.coding.best_of import (
    CHOSEN_KEY,
    MICRO_USD,
    PLAN_KEY,
    Attempt,
    AttemptStep,
    Candidate,
    RecordAttempt,
    SelectAttempt,
    SubtaskAttempts,
    allocate_attempts,
    candidate_key,
    parse_diff_stats,
)
from sca.kinds.coding.spec import BestOfSpec, CodingSpec
from sca.kinds.coding.subtasks import ChildActivities, ChildStore
from sca.workspace.overlay import OverlayWorkspace
from sca.workspace.snapshots import preparation_key

SNAPSHOT_KEY = "best_of_snapshot_key"
SNAPSHOT_PIN = "best-of-prepared"


class AttemptStore(ChildStore):
    def __init__(self, child: ChildStore, attempt: Attempt) -> None:
        super().__init__(child.store, child.name)
        self.attempt = attempt

    async def get_task(self, task_id: str) -> Task:
        task = await super().get_task(task_id)
        spec = {**task.spec, "model": self.attempt.model, "agent_version": self.attempt.agent_version,
                "max_cost_usd": self.attempt.max_cost_usd, "best_of": None}
        prefix = self.usage_prefix
        usage = {k.removeprefix(prefix): v for k, v in task.usage.items() if k.startswith(prefix)}
        return task.model_copy(update={"spec": spec, "usage": usage})

    async def add_usage(self, task_id: str, usage: dict[str, int]) -> None:
        # Keep tenant totals and the attempt's allowance in sync in one Store write.
        await self.store.add_usage(
            task_id, {**usage, **{self.usage_prefix + k: v for k, v in usage.items()}},
        )

    async def check_budget(self, task_id: str) -> str | None:
        reason = await self.store.check_budget(task_id)
        if reason:
            return reason
        usage = (await self.get_task(task_id)).usage
        tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        if self.attempt.max_tokens is not None and tokens >= self.attempt.max_tokens:
            return f"Attempt token budget exhausted ({self.attempt.max_tokens} tokens)"
        cost = self.attempt.max_cost_usd
        if cost is not None and usage.get("cost_micro_usd", 0) >= Decimal(str(cost)) * MICRO_USD:
            return f"Attempt cost budget exhausted ({cost} USD)"
        return None

    async def append_event(
        self, task_id: str, type_: str, payload: dict, dedup_key: str | None = None
    ) -> Event | None:
        if type_ == EventType.AGENT_TURN_STARTED:
            await self.update_result(task_id, {"turns": payload["turn"]})
        elif type_ == EventType.AGENT_TURN_COMPLETED and payload.get("cost_final"):
            await self.update_result(task_id, {"cost_recorded": True})
        elif type_ == EventType.WORKSPACE_CHECKPOINTED:
            await self.update_result(task_id, {"checkpoint_turn": payload["turn"]})
        elif type_ == EventType.VERIFICATION_COMPLETED:
            await self.update_result(task_id, {"verification": payload})
            if payload.get("log_artifact"):
                payload = {**payload, "log_artifact": self.artifact_name(payload["log_artifact"])}
        elif type_ == "intents.checked":
            await self.update_result(task_id, {"intent_verification": payload})
        number = self.attempt.attempt
        return await self.store.append_event(
            task_id, type_, {**payload, "best_of_attempt": number, "best_of_candidate": number},
            dedup_key=f"{self.name}.{dedup_key}" if dedup_key else None,
        )

    async def start_guided_turn(
        self, task_id: str, start: dict, key: str, message_ids: list[str], interrupted_ids: list[str],
        *, plan_attempt: int | None = None,
    ) -> None:
        await self.update_result(task_id, {"turns": start["turn"]})
        number = self.attempt.attempt
        await super().start_guided_turn(
            task_id, {**start, "best_of_attempt": number, "best_of_candidate": number},
            key, message_ids, interrupted_ids, plan_attempt=plan_attempt,
        )


class AttemptActivities(ChildActivities):
    def __init__(self, coding: CodingActivities, attempt: Attempt) -> None:
        super().__init__(coding, attempt.name)
        self.store = AttemptStore(self.store, attempt)

    def _agent_ctx(
        self, task_id: str, spec: ExecutionSpec, champion: dict | None = None,
    ) -> AgentContext:
        context = super()._agent_ctx(task_id, spec, champion)
        # The OpenCode brain pool uses this identity to route tools and session state.
        return context.model_copy(update={"task_id": f"{task_id}-{self.scope}"})

    async def run_verification(self, inp: StepInput) -> VerifyResult:
        if not (await self.store.get_task(inp.task_id)).result.get("intents"):
            return await super().run_verification(inp)
        verify, intents = await asyncio.gather(super().run_verification(inp), self.verify_intents(inp))
        return VerifyResult(verify.passed and intents.passed, verify.exit_code or intents.exit_code,
                            verify.output_tail + "\n" + intents.output_tail, verify.skipped)


class BestOfActivities:
    def __init__(self, coding: CodingActivities) -> None:
        self.coding = coding
        self.store = coding.store

    def all(self) -> list:
        return [self.start, self.step, self.record, self.adopt, self.cleanup,
                self.start_subtask, self.cleanup_subtask]

    def _for_subtask(self, name: str) -> BestOfActivities:
        return BestOfActivities(ChildActivities(self.coding, name))

    @activity.defn(name="coding.best_of_subtask_start")
    async def start_subtask(self, inp: SubtaskAttempts) -> list[Attempt]:
        scoped = self._for_subtask(inp.name)
        spec = await scoped.coding._spec(inp.task_id)
        task = await scoped.store.get_task(inp.task_id)
        remaining = spec.max_cost_usd
        if remaining is not None:
            spent = Decimal(task.usage.get("cost_micro_usd", 0)) / MICRO_USD
            remaining = float(max(Decimal(0), Decimal(str(remaining)) - spent))
        if scoped.coding._backend(spec).name == "fake":
            remaining = None  # Scripted turns do not use a model. Parent budget checks still apply.
        spec = spec.model_copy(update={"best_of": BestOfSpec(n=inp.n), "max_cost_usd": remaining})
        return await scoped._start(inp.task_id, spec, f"best-of:{inp.task_id}:{inp.name}")

    @activity.defn(name="coding.best_of_start")
    async def start(self, task_id: str) -> list[Attempt]:
        spec = await self.coding._spec(task_id)
        return await self._start(task_id, spec, f"best-of:{task_id}")

    async def _start(self, task_id: str, spec: CodingSpec, snapshot_scope: str) -> list[Attempt]:
        attempts = allocate_attempts(spec, self.coding.settings.default_model)
        if self.coding.gateway is None and any(a.max_cost_usd is not None for a in attempts):
            raise _non_retryable(InvalidTaskError("Best-of-N cost limits require the model gateway"))
        task = await self.store.get_task(task_id)
        ws = self.coding._workspace(task_id)
        base = task.result["base_sha"]
        key = preparation_key(task.tenant_id, snapshot_scope, base, "", "")
        sandbox_id = task.result["sandbox_id"]
        snapshot = await self.coding._snapshots().publish(
            key, self.coding.sandbox.download(sandbox_id, "/workspace/."), base_commit=base,
            saved_seconds=task.result.get("preparation_seconds", 0),
            archive_root="workspace" if self.coding.settings.sandbox_driver == "k8s" else "",
            retain_at=ws.task_dir / SNAPSHOT_PIN,
        )
        backend = self.coding._backend(spec)
        default_version = getattr(backend, "default_version", None) or getattr(backend, "version", None)
        if backend.name == "opencode-brain":
            default_version = self.coding.settings.opencode_default_version
        plan = [{**asdict(a), "agent_version": a.agent_version or default_version,
                 "turn_limit": spec.max_fix_attempts + 1} for a in attempts]
        await self.store.update_result(task_id, {SNAPSHOT_KEY: key, PLAN_KEY: plan})
        await self.store.append_event(
            task_id, "best_of.started",
            {"snapshot_id": snapshot["id"], "attempts": plan, "select": spec.best_of.select},
            dedup_key="best_of.started",
        )
        await self.coding.destroy_sandbox(task_id)
        return attempts

    @activity.defn(name="coding.best_of_step")
    async def step(self, inp: AttemptStep) -> dict:
        if inp.subtask is not None:
            return await self._for_subtask(inp.subtask).step(replace(inp, subtask=None))
        child = AttemptActivities(self.coding, inp.attempt)
        if inp.step.action == "prepare":
            await self._prepare(child, await self.store.get_task(inp.step.task_id))
            return {}
        return await child.run_step(inp.step)

    async def _prepare(self, child: AttemptActivities, task: Task) -> None:
        task_id = str(task.id)
        ws = child._workspace(task_id)
        base = task.result["base_sha"]
        await ws.prepare(str(self.coding._workspace(task_id).mirror), base,
                         mirror_only=True, tenant_id=task.tenant_id)
        snapshot = await self.coding._snapshots().fork(task.result[SNAPSHOT_KEY], ws.workspace)
        if snapshot is None or snapshot["base_commit"] != base:
            raise WorkspaceError("Prepared Best-of-N snapshot is unavailable")
        (ws.task_dir / "workspace.ready").write_text(base)
        await child.store.update_result(task_id, {
            "base_sha": base, "branch": ws.branch,
            **({"scope": task.result["scope"]} if "scope" in task.result else {}),
        })
        await child.store.append_event(task_id, "workspace.forked", {"snapshot_id": snapshot["id"]},
                                       dedup_key="workspace.forked")
        sandbox_id = await child.create_sandbox(task_id)
        exit_event, _ = await child._run(sandbox_id, [
            "git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "checkout", "-B", ws.branch, "HEAD",
        ], timeout_s=90)
        if exit_event.exit_code:
            raise ApplicationError("Best-of-N attempt branch initialization failed")
        await child._save_state(task_id, 0, base)

    @activity.defn(name="coding.best_of_record")
    async def record(self, inp: RecordAttempt) -> Candidate:
        if inp.subtask is not None:
            return await self._for_subtask(inp.subtask).record(replace(inp, subtask=None))
        task = await self.store.get_task(inp.task_id)
        attempt = inp.attempt.attempt
        plan = task.result[PLAN_KEY][attempt - 1]
        child = AttemptActivities(self.coding, inp.attempt)
        scoped = await child.store.get_task(inp.task_id)
        commit = inp.result.commit or None
        verification = stats = patch_name = None
        if commit:
            ws = child._workspace(inp.task_id)
            if not ws.mirror.exists():
                await child._ensure_state(inp.task_id)
            args = ["--git-dir", str(ws.mirror), "diff", "--no-ext-diff", "--no-textconv"]
            range_ = [task.result["base_sha"], commit, "--"]
            stats = parse_diff_stats(await ws._git(*args, "--numstat", "-z", *range_))
            patch = (await ws._git(*args, "--binary", *range_)).encode()
            patch_name = f"{inp.attempt.name}.patch"
            await self.coding._publish_artifact(inp.task_id, patch_name, patch, "text/x-diff")
            if isinstance(self.store, ChildStore):
                patch_name = self.store.artifact_name(patch_name)
            detail = scoped.result.get("verification", {})
            turn = scoped.result.get("checkpoint_turn")
            if detail and detail.get("turn") == turn:
                intents = scoped.result.get("intent_verification", {})
                verification = {"passed": inp.result.passed, "checks": detail.get("checks", []),
                                "skipped": detail.get("skipped", False)}
                if intents.get("turn") == turn:
                    verification["intent_checks"] = intents["checks"]
        micro_usd = scoped.usage.get("cost_micro_usd")
        cost = micro_usd / MICRO_USD if micro_usd is not None else None
        if cost is None and scoped.result.get("cost_recorded"):
            cost = 0.0
        candidate = Candidate(attempt, plan["model"], plan["agent_version"], commit, verification,
                              cost, scoped.result.get("turns", 0), stats, patch_name, inp.result.reason)
        data = asdict(candidate)
        await self.store.update_result(inp.task_id, {candidate_key(attempt): data})
        await self.store.append_event(inp.task_id, "best_of.attempt_completed", data,
                                      dedup_key=f"best_of.attempt_completed.{attempt}")
        return candidate

    @activity.defn(name="coding.best_of_adopt")
    async def adopt(self, inp: SelectAttempt) -> None:
        if inp.subtask is not None:
            await self._for_subtask(inp.subtask).adopt(replace(inp, subtask=None))
            return
        task_id = inp.task_id
        task = await self.store.get_task(task_id)
        candidate = task.result[candidate_key(inp.attempt)]
        commit = candidate["commit"]
        await self.coding._ensure_state(task_id)
        ws = self.coding._workspace(task_id)
        if task.result.get(CHOSEN_KEY) is None:
            child = ChildActivities(self.coding, f"best-of-{inp.attempt}")
            source = child._workspace(task_id)
            if not source.mirror.exists():
                await child._ensure_state(task_id)
            ref = f"refs/sca/best-of/{inp.attempt}"
            git = ["--git-dir", str(ws.mirror)]
            await ws._git(*git, "fetch", "--no-tags", "--", str(source.mirror),
                          f"+refs/heads/{source.branch}:{ref}")
            if (await ws._git(*git, "rev-parse", ref)).strip() != commit:
                raise WorkspaceError("Best-of-N attempt branch moved after checkpoint")
            await ws._git(*git, "update-ref", f"refs/heads/{ws.branch}", commit)
            if isinstance(ws, OverlayWorkspace):
                await ws.cleanup()
                source_file = ws.task_dir / "layer-source.json"
                metadata = json.loads(source_file.read_text())
                metadata.pop("snapshot_key", None)
                source_file.write_text(json.dumps(metadata))
                (ws.task_dir / "overlay.snapshot").unlink(missing_ok=True)
            (ws.task_dir / "workspace.ready").unlink(missing_ok=True)
            (ws.task_dir / "workspace.snapshot").unlink(missing_ok=True)
            await ws.build_workspace(commit)
            await self.coding._save_state(task_id, candidate["turns"], commit)
            await self.store.update_result(task_id, {CHOSEN_KEY: inp.attempt, "commit": commit,
                                                     "workspace_forked": False})
        await self.store.append_event(
            task_id, "best_of.selected", {"attempt": inp.attempt, "commit": commit, "actor": inp.actor},
            dedup_key="best_of.selected",
        )

    @activity.defn(name="coding.best_of_cleanup")
    async def cleanup(self, task_id: str) -> None:
        directory = self.coding._task_dir(task_id)
        (directory / f"{SNAPSHOT_PIN}.snapshot").unlink(missing_ok=True)
        with suppress(FileNotFoundError):
            (directory / SNAPSHOT_PIN).rmdir()
        for path in (directory / "sub").glob("best-of-[1-5]"):
            await asyncio.to_thread(remove_tree, path)

    @activity.defn(name="coding.best_of_subtask_cleanup")
    async def cleanup_subtask(self, inp: SubtaskAttempts) -> None:
        await self._for_subtask(inp.name).cleanup(inp.task_id)
