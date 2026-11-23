"""Activity-local child scope; shared activities keep their normal retry and usage behavior."""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sca.codegraph.index import CodeIndex
from sca.codegraph.repository import changed_paths, display_path, index_commit
from sca.core.errors import WorkspaceError
from sca.core.interfaces import ObjectStore, StateTransport
from sca.core.models import Event, EventType, SandboxSpec, Task
from sca.db.repo import Store
from sca.kinds.coding.activities import (
    CheckpointResult,
    CodingActivities,
    StepInput,
    TurnInput,
    branch_name,
)
from sca.kinds.coding.spec import path_scope
from sca.kinds.coding.subtask_inputs import SubtaskInput, SubtaskPreparationInput
from sca.workspace.git import GitWorkspace
from sca.workspace.merge import MERGED_STATUSES, SubtaskResult, merge_subtasks


def _artifact_name(scope: str, artifact: str, nested: bool) -> str:
    separator = "." if nested else "-"
    return f"subtask-{scope}{separator}{artifact}"


class ChildStore:
    def __init__(self, store: Store, name: str) -> None:
        self.store, self.name = store, name
        self.prefix = f"subtask.{name}."

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
            {**payload, "subtask": self.name},
            dedup_key=f"{self.name}.{dedup_key}" if dedup_key else None,
        )

    async def get_session_id(self, task_id: str) -> str | None:
        return (await self.get_task(task_id)).result.get("session_id")

    async def start_guided_turn(
        self, task_id: str, start: dict, key: str, message_ids: list[str], interrupted_ids: list[str],
        *, plan_attempt: int | None = None,
    ) -> None:
        await self.store.start_guided_turn(
            task_id, {**start, "subtask": self.name}, f"{self.name}.{key}",
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

    def __init__(self, parent: CodingActivities, name: str) -> None:
        super().__init__(
            settings=parent.settings,
            store=ChildStore(parent.store, name),
            sandbox=parent.sandbox,
            agent=parent.agent,
            objects=ChildObjects(parent.objects, name),
            gateway=parent.gateway,
            secrets=parent.secrets,
            backends=parent.backends,
            worker_identity=parent.worker_identity,
        )
        self.name = name
        self._parent = parent if isinstance(parent, ChildActivities) else None

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
                TurnInput(inp.task_id, inp.turn, inp.prompt, inp.session_id)
            )
        else:
            result = await getattr(self, inp.action)(StepInput(inp.task_id, inp.turn))
        if inp.action == "check_budget":
            return {"reason": result}
        return asdict(result)

    def _task_dir(self, task_id: str) -> Path:
        base = (self._parent._task_dir(task_id) if self._parent is not None
                else self.settings.task_dir(task_id))
        return base / "sub" / self.name

    def _workspace(self, task_id: str) -> GitWorkspace:
        return GitWorkspace(self._task_dir(task_id), f"{branch_name(task_id)}-{self.name}")

    async def _create_sandbox(self, task_id: str) -> tuple[str, str]:
        # The provider wrapper changes only identity and labels, retaining all mounts/policy.
        provider = self.sandbox
        name = self.scope
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
        return f"task-{task_id}-{self.name}"

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
        shared = set(scope["shared_paths"])
        disallowed = []
        for raw in await changed_paths(ws.mirror, approved, commit):
            try:
                path = raw.decode("utf-8")
            except UnicodeDecodeError:
                disallowed.append(display_path(raw))
                continue
            if path not in shared and not matcher.match_file(path, separators=()):
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
        sandbox_id = task.result.get("sandbox_id", f"sca-{self._scoped_sandbox_name(task_id)}")
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

    @activity.defn(name="coding.prepare_subtask_base")
    async def prepare_base(self, task_id: str) -> None:
        # Prepare once before fan-out so children do not concurrently restore the parent mirror.
        await self.acts._ensure_state(task_id)

    @activity.defn(name="coding.prepare_scoped_subtask")
    async def prepare(self, inp: SubtaskPreparationInput) -> dict:
        task_id = inp.task_id
        acts = self.acts
        await acts._ensure_state(task_id)
        parent = await acts.store.get_task(task_id)
        child = ChildActivities(acts, inp.name)
        await child._ensure_state(task_id)
        ws = child._workspace(task_id)
        task = await child.store.get_task(task_id)
        base = task.result.get("base_sha")
        if not base or not ws.mirror.exists():
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
            "scope": {"paths": inp.scopes.get(inp.name, []), "shared_paths": inp.shared_paths},
        })
        hints = ""
        if inp.scopes or inp.shared_paths:
            with contextlib.suppress(OSError, WorkspaceError):
                index = await index_commit(
                    ws.mirror, base, acts.settings.data_dir / "layers", parent.tenant_id
                )
                hints = contract_hints(index, inp.scopes, inp.shared_paths)
        await child.store.append_event(
            task_id, EventType.SUBTASK_STARTED, child._activity_route(), dedup_key="started"
        )
        await child.create_sandbox(task_id)
        return {"contracts": hints}

    async def _merge_dependencies(
        self, inp: SubtaskPreparationInput, ws: GitWorkspace, base: str,
    ) -> tuple[str, list[SubtaskResult]]:
        dependencies = []
        for result in inp.dependencies:
            child = ChildActivities(self.acts, result.name)
            await child._ensure_state(inp.task_id)
            # A dependency without its own edits may still carry the merged work of its ancestors.
            dependencies.append((
                child._workspace(inp.task_id).mirror,
                replace(result, changed=bool(result.commit), depends_on=[]),
            ))
        return await merge_subtasks(ws.mirror, base, ws.branch, dependencies, task_id=inp.task_id)
