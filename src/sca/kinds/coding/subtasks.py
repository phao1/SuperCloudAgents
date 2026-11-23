"""Activity-local child scope; shared activities keep their normal retry and usage behavior."""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sca.codegraph.index import CodeIndex
from sca.codegraph.repository import changed_paths, display_path, index_commit
from sca.core.errors import WorkspaceError
from sca.core.interfaces import ObjectStore, StateTransport
from sca.core.models import Event, EventType, SandboxSpec, Task, tail
from sca.db.repo import Store
from sca.history.privacy import clean_text
from sca.kinds.coding.activities import (
    CheckpointResult,
    CodingActivities,
    StepInput,
    TurnInput,
    branch_name,
)
from sca.kinds.coding.spec import path_scope
from sca.kinds.coding.subtask_inputs import (
    SubtaskAgent,
    SubtaskInput,
    SubtaskPreparationInput,
    SubtaskProgress,
    SubtaskSchedule,
)
from sca.live_heartbeat import LaneScope
from sca.workspace.git import GitWorkspace
from sca.workspace.merge import MERGED_STATUSES, SubtaskResult, merge_subtasks


def _artifact_name(scope: str, artifact: str, nested: bool) -> str:
    separator = "." if nested else "-"
    return f"subtask-{scope}{separator}{artifact}"


class ChildStore:
    def __init__(self, store: Store, name: str, agent: SubtaskAgent | None = None) -> None:
        self.store, self.name = store, name
        self.prefix = f"subtask.{name}."
        self._agent_fields = agent.event_fields() if agent else {}

    @property
    def usage_prefix(self) -> str:
        if isinstance(self.store, ChildStore):
            return self.store.usage_prefix + self.prefix
        return self.prefix

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    async def get_task(self, task_id: str) -> Task:
        task = await self.store.get_task(task_id)
        result = {
            k.removeprefix(self.prefix): v
            for k, v in task.result.items()
            if k.startswith(self.prefix)
        }
        for key in ("prompt_version", "memory_context", "memory_note", "intents"):
            if key in task.result:
                result[key] = task.result[key]
        return task.model_copy(update={"result": result})

    async def update_result(self, task_id: str, patch: dict) -> None:
        await self.store.update_result(task_id, {self.prefix + k: v for k, v in patch.items()})

    async def append_event(
        self,
        task_id: str,
        type_: str,
        payload: dict,
        dedup_key: str | None = None,
    ) -> Event | None:
        return await self.store.append_event(
            task_id,
            type_,
            {**payload, "subtask": self.name, **self._agent_fields},
            dedup_key=f"{self.name}.{dedup_key}" if dedup_key else None,
        )

    async def get_session_id(self, task_id: str) -> str | None:
        return (await self.get_task(task_id)).result.get("session_id")

    async def start_guided_turn(
        self, task_id: str, start: dict, key: str, message_ids: list[str], interrupted_ids: list[str],
        *, plan_attempt: int | None = None,
    ) -> None:
        await self.store.start_guided_turn(
            task_id, {**start, "subtask": self.name, **self._agent_fields}, f"{self.name}.{key}",
            message_ids, interrupted_ids, plan_attempt=plan_attempt,
        )

    async def set_session(self, task_id: str, backend: str, backend_session_id: str) -> None:
        await self.update_result(task_id, {"session_id": backend_session_id})

    async def add_artifact(self, task_id: str, name: str, *args: Any) -> Any:
        name = _artifact_name(self.name, name, isinstance(self.store, ChildStore))
        return await self.store.add_artifact(task_id, name, *args)

    def artifact_name(self, name: str) -> str:
        name = _artifact_name(self.name, name, isinstance(self.store, ChildStore))
        return self.store.artifact_name(name) if isinstance(self.store, ChildStore) else name


class ChildObjects:
    def __init__(self, objects: ObjectStore, name: str) -> None:
        self.objects, self.name = objects, name

    def _key(self, key: str) -> str:
        return key.replace("/state/", f"/subtasks/{self.name}/state/")

    async def get(self, key: str) -> bytes:
        return await self.objects.get(self._key(key))

    async def put(self, key: str, *args: Any) -> str:
        if "/state/" in key:
            return await self.objects.put(self._key(key), *args)
        path = Path(key)
        name = _artifact_name(self.name, path.name, isinstance(self.objects, ChildObjects))
        return await self.objects.put(str(path.with_name(name)), *args)


@dataclass
class ScopedCheckpointResult(CheckpointResult):
    scope_feedback: str | None = None


class _CheckpointTransport:
    def __init__(
        self, original: StateTransport, pull: Callable[[str, GitWorkspace], Awaitable[str]],
    ) -> None:
        self.original = original
        self.pull_commit = pull

    def __getattr__(self, name: str) -> Any:
        return getattr(self.original, name)


class ChildActivities(CodingActivities):
    # Instances restored from before nested subtasks have no parent in their state.
    _parent: ChildActivities | None = None
    lane: SubtaskAgent | None = None

    def __init__(self, parent: CodingActivities, name: str, agent: SubtaskAgent | None = None) -> None:
        super().__init__(
            settings=parent.settings,
            store=ChildStore(parent.store, name, agent),
            sandbox=parent.sandbox,
            agent=parent.agent,
            objects=ChildObjects(parent.objects, name),
            gateway=parent.gateway,
            secrets=parent.secrets,
            backends=parent.backends,
            worker_identity=parent.worker_identity,
        )
        self.name = name
        self.lane = agent
        self._parent = parent if isinstance(parent, ChildActivities) else None

    @property
    def workspace_name(self) -> str:
        return self.lane.workspace if self.lane else self.name

    def _live_scope(self) -> LaneScope:
        return self.name, None

    @property
    def scope(self) -> str:
        if self._parent is None:
            return self.name
        names = f"{self._parent.name}/{self.name}".encode()
        return "nested." + hashlib.sha256(names).hexdigest()[:12]

    async def run_step(self, inp: SubtaskInput) -> dict:
        if inp.action in {"run_setup", "destroy_sandbox"}:
            await getattr(self, inp.action)(inp.task_id)
            return {}
        if inp.action == "collect_memory_proposals":
            await self.collect_memory_proposals(StepInput(inp.task_id, inp.turn))
            return {}
        if inp.action == "run_agent_turn":
            result = await self.run_agent_turn(
                TurnInput(inp.task_id, inp.turn, inp.prompt, inp.session_id,
                          inp.interjectible, inp.guidance)
            )
            if self.lane is not None and not result.session_id and not result.skipped:
                await self.store.update_result(inp.task_id, {
                    "turn_summary": tail(clean_text(result.final_message), 2000),
                })
        else:
            result = await getattr(self, inp.action)(StepInput(inp.task_id, inp.turn))
        if inp.action == "check_budget":
            return {"reason": result}
        return asdict(result)

    def _task_dir(self, task_id: str) -> Path:
        base = (self._parent._task_dir(task_id) if self._parent is not None
                else self.settings.task_dir(task_id))
        return base / "sub" / self.workspace_name

    async def record_started(self, task_id: str) -> None:
        spec = await self._spec(task_id)
        await self.store.append_event(
            task_id, EventType.SUBTASK_STARTED,
            {"backend": self._backend(spec).name, "model": spec.model or self.settings.default_model,
             **self._activity_route()},
            dedup_key="started",
        )

    def _workspace(self, task_id: str) -> GitWorkspace:
        return GitWorkspace(self._task_dir(task_id), f"{branch_name(task_id)}-{self.workspace_name}")

    async def _create_sandbox(self, task_id: str) -> tuple[str, str]:
        # The provider wrapper changes only identity and labels, retaining all mounts/policy.
        provider = self.sandbox
        name = self.workspace_name if self.lane else self.scope
        sandbox_name = self._scoped_sandbox_name(task_id)

        class ScopedProvider:
            def __getattr__(self, attr: str) -> Any:
                return getattr(provider, attr)

            async def create(self, _sandbox_name: str, spec: SandboxSpec) -> str:
                spec.labels["sca.subtask"] = name
                return await provider.create(sandbox_name, spec)

        self.sandbox = ScopedProvider()
        try:
            return await super()._create_sandbox(task_id)
        finally:
            self.sandbox = provider

    def _scoped_sandbox_name(self, task_id: str) -> str:
        if self._parent is not None:
            return f"{self.scope.replace('.', '-')}-{task_id}"
        return f"task-{task_id}-{self.workspace_name}"

    def _memory_proposal_batch(self, turn: int) -> str:
        return f"subtask.{self.name}.{turn}"

    async def checkpoint_workspace(self, inp: StepInput) -> CheckpointResult:
        task = await self.store.get_task(inp.task_id)
        scope = task.result.get("scope")
        if not scope or not scope["paths"]:
            return await super().checkpoint_workspace(inp)

        original = self.transport
        feedback = None

        async def pull(sandbox_id: str, ws: GitWorkspace) -> str:
            nonlocal feedback
            commit, feedback = await self._scoped_commit(inp, task, sandbox_id, ws, original)
            return commit

        # Guard the fetch before the normal checkpoint publishes its bundle, commit and event.
        self.transport = _CheckpointTransport(original, pull)
        try:
            result = await super().checkpoint_workspace(inp)
        finally:
            self.transport = original
        return ScopedCheckpointResult(result.commit, result.changed, feedback)

    async def _scoped_commit(
        self, inp: StepInput, task: Task, sandbox_id: str, ws: GitWorkspace,
        transport: StateTransport,
    ) -> tuple[str, str | None]:
        approved = task.result.get("commit") or task.result["base_sha"]
        candidate = GitWorkspace(ws.task_dir, "sca/scope-candidate")
        decision_key = f"scope_turn_{inp.turn}"
        violation = task.result.get(decision_key)
        git = ["--git-dir", str(ws.mirror)]
        try:
            commit = await transport.pull_commit(sandbox_id, candidate)
            if violation is None:
                violation = await self._scope_violation(ws, task.result["scope"], approved, commit)
                if violation is not None:
                    await self.store.update_result(inp.task_id, {decision_key: violation})
            if violation is not None:
                await self.store.append_event(
                    inp.task_id, "scope.violation", {"turn": inp.turn, **violation},
                    dedup_key=f"turn{inp.turn}.scope_violation",
                )
                exit_event, _ = await self._run(
                    sandbox_id,
                    ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                     "reset", "--hard", approved],
                    timeout_s=90,
                )
                if exit_event.exit_code:
                    raise ApplicationError("could not roll back rejected subtask turn")
                commit = approved
            await ws._git(*git, "update-ref", f"refs/heads/{ws.branch}", commit)
        finally:
            await ws._git(*git, "update-ref", "-d", f"refs/heads/{candidate.branch}")
        return commit, _scope_feedback(violation) if violation is not None else None

    async def _scope_violation(
        self, ws: GitWorkspace, scope: dict, approved: str, commit: str,
    ) -> dict | None:
        matcher = path_scope(scope["paths"])
        parallel = [path_scope(paths) for paths in scope.get("parallel_paths", [])]
        shared = set(scope["shared_paths"])
        disallowed = []
        for raw in await changed_paths(ws.mirror, approved, commit):
            try:
                path = raw.decode("utf-8")
            except UnicodeDecodeError:
                disallowed.append(display_path(raw))
                continue
            if (path not in shared and not matcher.match_file(path, separators=())
                    or any(other.match_file(path, separators=()) for other in parallel)):
                disallowed.append(path)
        if disallowed:
            return {"paths": sorted(disallowed), "reason": "outside_scope"}
        if commit != approved:
            parents = await ws._git("--git-dir", str(ws.mirror),
                                    "rev-list", "--parents", "-n", "1", commit)
            if parents.split() != [commit, approved]:
                return {"paths": [], "reason": "unexpected_git_history"}
        return None

    async def destroy_sandbox(self, task_id: str) -> None:
        task = await self.store.get_task(task_id)
        sandbox_id = task.result.get("sandbox_id")
        if not sandbox_id and self.workspace_name != self.name:
            owner = await ChildStore(self.store.store, self.workspace_name).get_task(task_id)
            sandbox_id = owner.result.get("sandbox_id")
        if not sandbox_id:
            name = self._scoped_sandbox_name(task_id)
            sandbox_id = name if self.lane and self.settings.sandbox_driver == "local" else f"sca-{name}"
        await self.sandbox.destroy(sandbox_id)
        await self.store.append_event(
            task_id,
            EventType.SANDBOX_DESTROYED,
            {"sandbox_id": sandbox_id},
            dedup_key="sandbox.destroyed",
        )


def _scope_feedback(violation: dict) -> str:
    if violation["reason"] == "outside_scope":
        paths = violation["paths"]
        details = json.dumps(paths[:24], ensure_ascii=False)[:2000]
        reason = f"Paths outside your subtask scope ({len(paths)}): {details}."
    else:
        reason = "The turn created Git history outside the platform checkpoint."
    return (f"{reason} Your last turn was discarded. Reapply the intended edits inside your "
            "scope and leave Git commits to the platform.")


def contract_hints(index: CodeIndex, scopes: dict[str, list[str]], shared_paths: list[str]) -> str:
    matchers = {name: path_scope(patterns) for name, patterns in scopes.items() if patterns}
    users: dict[str, set[str]] = {}
    for path in index.files:
        owners = {name for name, scope in matchers.items() if scope.match_file(path, separators=())}
        if owners:
            for module in [path, *index.dependencies.get(path, ())]:
                users.setdefault(module, set()).update(owners)
    shared = set(shared_paths) | {path for path, owners in users.items() if len(owners) > 1}
    lines = []
    for path in sorted(shared):
        table = index.symbols.get(path)
        if table and table.signatures:
            label = json.dumps(path, ensure_ascii=False)
            lines.extend(f"- {label} {signature[:350]}" for signature in table.signatures[:8])
    if not lines:
        return ""
    return ("\n\nShared Python contracts you must not break "
            "(ellipsis marks a default or literal)\n" + "\n".join(lines))[:6000]


class SubtaskActivities:
    def __init__(self, acts: CodingActivities) -> None:
        self.acts = acts

    # Parallel coding subtasks
    @activity.defn(name="coding.record_subtask_schedule")
    async def record_schedule(self, inp: SubtaskSchedule) -> str | None:
        payload: dict[str, Any] = {"max_parallel_agents": inp.max_parallel_agents}
        backend = None
        if inp.lanes:
            backend = self.acts._backend(await self.acts._spec(inp.task_id)).name
            payload["subtasks"] = [
                {"name": name, **SubtaskAgent(number, backend, names[0]).event_fields()}
                for number, names in enumerate(inp.lanes, start=1) for name in names
            ]
        await self.acts.store.append_event(
            inp.task_id, "subtasks.scheduled", payload,
            dedup_key="subtasks.scheduled",
        )
        return backend

    @activity.defn(name="coding.record_subtask_progress")
    async def record_progress(self, inp: SubtaskProgress) -> None:
        if inp.stage == "queued":
            payload = {}
        else:
            payload = {"passed": inp.passed, **({"status": "blocked"} if inp.blocked else {})}
            if inp.agent is None:
                spec = await self.acts._spec(inp.task_id)
                payload["backend"] = self.acts._backend(spec).name
        await ChildStore(self.acts.store, inp.name, inp.agent).append_event(
            inp.task_id, f"subtask.{inp.stage}", payload, dedup_key=inp.stage,
        )

    @activity.defn(name="coding.prepare_subtask_base")
    async def prepare_base(self, task_id: str) -> None:
        # Prepare once before fan-out so children do not concurrently restore the parent mirror.
        await self.acts._ensure_state(task_id)

    @activity.defn(name="coding.prepare_scoped_subtask")
    async def prepare(self, inp: SubtaskPreparationInput) -> dict:
        async with self.acts._live_task(inp.task_id, "workspace", scope=(inp.name, None)):
            return await self._prepare(inp)

    async def _prepare(self, inp: SubtaskPreparationInput) -> dict:
        task_id = inp.task_id
        acts = self.acts
        await acts._ensure_state(task_id)
        parent = await acts.store.get_task(task_id)
        child = ChildActivities(acts, inp.name, inp.agent)
        ws = child._workspace(task_id)
        task = await child.store.get_task(task_id)
        base = task.result.get("base_sha")
        continuation = {}
        continuing = inp.agent is not None and inp.agent.workspace != inp.name
        if continuing:
            previous = inp.dependencies[0]
            base = previous.commit
            continuation = await self._continue_lane(child, task_id, previous)
        else:
            await child._ensure_state(task_id)
        if not continuing and (not base or not ws.mirror.exists()):
            base = parent.result["base_sha"]
            await ws.prepare(str(acts._workspace(task_id).mirror), base, mirror_only=True)
            if inp.dependencies:
                base, merged = await self._merge_dependencies(inp, ws, base)
                blocked = [r.name for r in merged if r.status not in MERGED_STATUSES]
                if blocked:
                    return {"blocked": "Could not merge dependencies: " + ", ".join(blocked)}
            await ws.build_workspace(base)
            await child._save_state(task_id, 0, base)
        await child.store.update_result(task_id, {
            "base_sha": base,
            "branch": ws.branch,
            "scope": {"paths": inp.scopes.get(inp.name, []), "shared_paths": inp.shared_paths,
                      **({"parallel_paths": inp.parallel_paths} if inp.parallel_paths else {})},
        })
        hints = ""
        if inp.scopes or inp.shared_paths:
            with contextlib.suppress(OSError, WorkspaceError):
                index = await index_commit(
                    ws.mirror, base, acts.settings.data_dir / "layers", parent.tenant_id
                )
                hints = contract_hints(index, inp.scopes, inp.shared_paths)
        await child.record_started(task_id)
        if not continuing:
            await child.create_sandbox(task_id)
        return {"contracts": hints, **continuation}

    async def _continue_lane(self, child: ChildActivities, task_id: str, previous: SubtaskResult) -> dict:
        prior = ChildActivities(self.acts, previous.name, child.lane)
        restored = await prior._ensure_state(task_id)
        task = await prior.store.get_task(task_id)
        sandbox_id = task.result.get("sandbox_id")
        if restored or not (sandbox_id and await prior.sandbox.exists(sandbox_id)):
            if sandbox_id:
                await prior.sandbox.destroy(sandbox_id)
            await prior.restore_sandbox(StepInput(task_id, 0))
            sandbox_id = await prior._sandbox_id(task_id)

        # Verification may have left changes after the checkpoint. Reset in the sandbox.
        git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
        branch = child._workspace(task_id).branch
        for args in (["checkout", "-f", "-B", branch, previous.commit], ["clean", "-ffd"]):
            exit_event, _ = await child._run(sandbox_id, [*git, *args], timeout_s=90)
            if exit_event.exit_code:
                raise ApplicationError("could not prepare the next subtask from the lane checkpoint")
        session = task.result.get("session_id")
        await child.store.update_result(task_id, {"sandbox_id": sandbox_id, "session_id": session})
        await child._save_state(task_id, 0, previous.commit)
        return {"session_id": session, "previous_summary": task.result.get("turn_summary", "")}

    async def _merge_dependencies(
        self, inp: SubtaskPreparationInput, ws: GitWorkspace, base: str,
    ) -> tuple[str, list[SubtaskResult]]:
        dependencies = []
        for result in inp.dependencies:
            # A dependency without its own edits may still carry the merged work of its ancestors.
            dependencies.append(replace(result, changed=bool(result.commit), depends_on=[]))
        async with subtask_sources(self.acts, inp.task_id, dependencies) as sources:
            return await merge_subtasks(ws.mirror, base, ws.branch, sources, task_id=inp.task_id)


# Agent lanes
@contextlib.asynccontextmanager
async def subtask_sources(
    acts: CodingActivities, task_id: str, results: list[SubtaskResult],
) -> AsyncIterator[list[tuple[Path, SubtaskResult]]]:
    with TemporaryDirectory(prefix="sca-subtasks-") as directory:
        sources = []
        for result in results:
            child = ChildActivities(acts, result.name)
            source = child._workspace(task_id).mirror
            if result.agent_lane is None:
                await child._ensure_state(task_id)
            elif result.commit:
                # A later subtask may already be updating the same lane branch.
                source = Path(directory) / f"{result.name}.bundle"
                source.write_bytes(await child._state(task_id).checkpoint_bundle(result.commit))
            sources.append((source, result))
        yield sources
