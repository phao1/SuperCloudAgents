"""Activities of the coding workflow: every step that does I/O (design.md §7.3)."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from typing import Literal

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sca.config import Settings
from sca.core.errors import InvalidTaskError
from sca.core.interfaces import AgentBackend
from sca.core.models import (
    TERMINAL_STATES,
    AgentContext,
    EventType,
    ExecExit,
    RepoCredential,
    SandboxAdditions,
    TaskState,
    tail,
)
from sca.kernel.activities import (
    _NON_RETRYABLE,
    CheckpointResult,
    KernelActivities,
    PublishInput,
    SetStateInput,
    StepInput,
    TurnInput,
    TurnResult,
    VerifyResult,
    WorkspaceInfo,
    _non_retryable,
    branch_name,
    sandbox_name,
)
from sca.kinds.coding.spec import CodingSpec
from sca.kinds.coding.subtask_inputs import MergeInput, SubtaskInput
from sca.observability import business_metrics, metric_guard
from sca.storage.local import key_for
from sca.workspace.git import GitWorkspace
from sca.workspace.snapshots import SnapshotStore


class DependencyBackend:
    def __init__(self, backend: AgentBackend, settings: Settings) -> None:
        self.backend = backend
        self.settings = settings

    def __getattr__(self, name: str):
        return getattr(self.backend, name)

    def sandbox_additions(self, ctx: AgentContext) -> SandboxAdditions:
        from sca.agents.opencode_build import dependency_paths
        from sca.core.models import Mount

        additions = self.backend.sandbox_additions(ctx)
        cache = self.settings.opencode_src_cache.resolve()
        for modules in dependency_paths(self.settings):
            if (not modules.is_dir() or modules.is_symlink()
                    or not modules.resolve().is_relative_to(cache)):
                raise InvalidTaskError("OpenCode dependency cache missing or invalid")
            additions.mounts.append(Mount(
                source=str(modules), target=f"/workspace/{modules.relative_to(cache)}", read_only=True
            ))
        return additions


@dataclass
class CiRequest:
    commit: str
    branch: str


@dataclass
class CiOutcome:
    task_id: str
    turn: int
    commit: str
    passed: bool
    summary: str | None = None
    url: str | None = None
    timed_out: bool = False


# SCM
SCM_ATTEMPTS = 3
ScmPhase = Literal["review", "approve", "reject", "terminal"]


@dataclass
class ScmInput:
    task_id: str
    phase: ScmPhase


def commit_message(subject: str, task_id: str) -> str:
    """The trailer lets an external CI pipeline report back about this commit (deploy/ci)."""
    return f"{subject}\n\nSca-Task-Id: {task_id}"


PATCH_ARTIFACT = "changes.patch"


class CodingActivities(KernelActivities):
    def _workspace(self, task_id: str) -> GitWorkspace:
        from sca.workspace.layers import LayerStore

        ws = super()._workspace(task_id)
        if (self.settings.workspace_overlay and self.settings.sandbox_driver == "docker"
                and self.settings.state_transport == "mount"):
            from sca.workspace.overlay import OverlayWorkspace

            return OverlayWorkspace(ws.task_dir, ws.branch, cache=ws.cache,
                                    image=self.settings.sandbox_image,
                                    docker_bin=self.settings.docker_bin)
        if (not self.settings.workspace_overlay and self.settings.workspace_layers and self.settings.sandbox_driver == "docker"
                and self.settings.state_transport == "mount"):
            ws.layers = LayerStore(self.settings.data_dir / "layers")
        return ws

    async def _workspace_usage(self, task_id: str) -> dict:
        from sca.workspace.layers import disk_bytes

        ws = self._workspace(task_id)
        usage = {"workspace_bytes": await disk_bytes(getattr(ws, "upper", ws.workspace)),
                 "workspace_method": ("overlay" if hasattr(ws, "volume") else
                                      "reflink-auto" if ws.layers else "clone")}
        usage["method"] = usage["workspace_method"]
        await self.store.update_result(task_id, usage)
        return usage

    def _backend(self, spec: CodingSpec) -> AgentBackend:
        backend = super()._backend(spec)
        if (spec.repo_url == self.settings.opencode_repo_url
                and spec.image == self.settings.opencode_sandbox_image):
            if self.settings.state_transport != "mount":
                raise InvalidTaskError("OpenCode fix tasks require mount state transport")
            return DependencyBackend(backend, self.settings)
        return backend

    async def _spec(self, task_id: str) -> CodingSpec:
        task = await self.store.get_task(task_id)
        return CodingSpec.model_validate({
            **task.spec,
            "prompt_version": task.spec.get("prompt_version") or task.result.get("prompt_version"),
        })

    async def _repo_credential(self, tenant_id: str, repo_url: str) -> RepoCredential | None:
        credential = await self.secrets.repo_credential(tenant_id, repo_url)
        # Missing OpenBao entries mean anonymous access, never the host's shared identity.
        if credential is None and self.settings.secret_provider == "openbao":
            return RepoCredential(username="", password="")
        return credential

    def all(self) -> list:
        return [
            self.prepare_mirror,
            self.subtask_step,
            self.merge_subtasks,
            self.build_merged_workspace,
            self.prepare_workspace,
            self.checkpoint_workspace,
            self.run_verification,
            self.publish,
            self.request_ci,
            self.record_ci,
            self.sync_scm,
        ]

    @activity.defn(name="coding.prepare_mirror")
    async def prepare_mirror(self, task_id: str) -> WorkspaceInfo:
        await self._ensure_state(task_id)
        spec = await self._spec(task_id)
        ws = self._workspace(task_id)
        task = await self.store.get_task(task_id)
        if task.result.get("base_sha") and ws.mirror.exists():
            info = WorkspaceInfo(task.result["base_sha"], ws.branch)
        else:
            prepared = await ws.prepare(
                spec.repo_url,
                spec.base_ref,
                mirror_only=True,
                credential=await self._repo_credential(task.tenant_id, spec.repo_url),
                tenant_id=task.tenant_id,
            )
            info = WorkspaceInfo(prepared.base_sha, prepared.branch)
            await self.store.update_result(task_id, asdict(info))
        await self.store.append_event(
            task_id,
            EventType.WORKSPACE_PREPARED,
            {**asdict(info), "mirror_only": True},
            dedup_key="workspace.prepared",
        )
        await self._save_state(task_id, 0, info.base_sha)
        return info

    @activity.defn(name="coding.subtask_step")
    async def subtask_step(self, inp: SubtaskInput) -> dict:
        from sca.kinds.coding.subtasks import ChildActivities

        child = ChildActivities(self, inp.name)
        await self._ensure_state(inp.task_id)
        if inp.action == "prepare":
            await child.store.append_event(
                inp.task_id, EventType.SUBTASK_STARTED, child._activity_route(), dedup_key="started"
            )
            await child._ensure_state(inp.task_id)
            parent = await self.store.get_task(inp.task_id)
            ws = child._workspace(inp.task_id)
            task = await child.store.get_task(inp.task_id)
            if not task.result.get("base_sha"):
                await ws.prepare(
                    str(self._workspace(inp.task_id).mirror), parent.result["base_sha"]
                )
                await child.store.update_result(
                    inp.task_id, {"base_sha": parent.result["base_sha"], "branch": ws.branch}
                )
            await child._save_state(inp.task_id, 0, parent.result["base_sha"])
            await child.create_sandbox(inp.task_id)
            return {}
        return await child.run_step(inp)

    @activity.defn(name="coding.merge_subtasks")
    async def merge_subtasks(self, inp: MergeInput) -> list[dict]:
        await self._ensure_state(inp.task_id)
        from sca.workspace.merge import merge_subtasks

        task = await self.store.get_task(inp.task_id)
        from sca.kinds.coding.subtasks import ChildActivities

        for result in inp.results:
            await ChildActivities(self, result.name)._ensure_state(inp.task_id)
        ws = self._workspace(inp.task_id)
        commit, results = await merge_subtasks(
            ws.mirror,
            task.result["base_sha"],
            ws.branch,
            [(self._task_dir(inp.task_id) / "sub" / r.name / "mirror.git", r) for r in inp.results],
            task_id=inp.task_id,
        )
        for r in results:
            if r.status not in {"merged", "unchanged"}:
                child = GitWorkspace(self._task_dir(inp.task_id) / "sub" / r.name, r.branch)
                patch = (await child.diff(task.result["base_sha"]) if r.commit else "").encode()
                name = f"subtask-{r.name}.patch"
                uri = await self.objects.put(key_for(inp.task_id, name), patch, "text/x-diff")
                await self.store.add_artifact(inp.task_id, name, "text/x-diff", uri, len(patch))
        await self._save_state(inp.task_id, 0, commit)
        statuses = [asdict(r) for r in results]
        await self.store.update_result(inp.task_id, {"commit": commit, "subtasks": statuses})
        await self.store.append_event(
            inp.task_id,
            EventType.SUBTASKS_MERGED,
            {"subtasks": statuses},
            dedup_key="subtasks.merged",
        )
        return statuses

    @activity.defn(name="coding.build_merged_workspace")
    async def build_merged_workspace(self, task_id: str) -> None:
        await self._ensure_state(task_id)
        task = await self.store.get_task(task_id)
        ws = self._workspace(task_id)
        # Restoring state may already have built the pre-merge commit and marked it ready;
        # the merged commit must replace it.
        (ws.task_dir / "workspace.ready").unlink(missing_ok=True)
        await ws.build_workspace(task.result["commit"])

    @activity.defn(name="coding.prepare_workspace")
    async def prepare_workspace(self, task_id: str) -> WorkspaceInfo:
        if await self._ensure_state(task_id):
            task = await self.store.get_task(task_id)
            return WorkspaceInfo(task.result["base_sha"], self._workspace(task_id).branch)
        spec = await self._spec(task_id)
        started = time.monotonic()
        try:
            task = await self.store.get_task(task_id)
            ws = self._workspace(task_id)
            enabled = self._snapshots_enabled(spec)
            prepared = await ws.prepare(
                spec.repo_url,
                spec.base_ref,
                mirror_only=enabled,
                tenant_id=task.tenant_id,
                credential=await self._repo_credential(task.tenant_id, spec.repo_url),
            )
            if enabled:
                key = await self._snapshot_key(task_id, prepared.base_sha)
                snapshot = await self._snapshots().fork(
                    key, ws.overlay if hasattr(ws, "volume") else ws.workspace,
                    overlay=hasattr(ws, "volume"))
                if snapshot is None:
                    prepared = await ws.build_workspace(prepared.base_sha)
                else:
                    if hasattr(ws, "volume"):
                        source_file = ws.task_dir / "layer-source.json"
                        source_file.write_text(json.dumps({**json.loads(source_file.read_text()),
                                                           "snapshot_key": key}))
                        ws.overlay.mkdir(parents=True, exist_ok=True)
                        for name in ("upper", "work"):
                            (ws.overlay / name).mkdir(exist_ok=True)
                        (ws.task_dir / "workspace.overlay").write_text(json.dumps({
                            "volume": ws.volume,
                            "lower": str(self._snapshots().root / snapshot["id"])}))
                        ws.workspace.mkdir(exist_ok=True)
                        await ws.ensure_volume()
                    (ws.task_dir / "workspace.ready").write_text(prepared.base_sha)
                    await self.store.update_result(task_id, {
                        "snapshot_id": snapshot["id"], "workspace_forked": True,
                        "snapshot_bytes": snapshot["size_bytes"],
                    })
                    await self.store.append_event(
                        task_id, "workspace.forked",
                        {"snapshot_id": snapshot["id"],
                         "saved_seconds": snapshot["saved_seconds"],
                         "fork_seconds": time.monotonic() - started},
                        dedup_key="workspace.forked")
            await self.store.update_result(task_id, {
                "preparation_seconds": time.monotonic() - started})
        except InvalidTaskError as e:
            raise _non_retryable(e) from e
        info = WorkspaceInfo(base_sha=prepared.base_sha, branch=prepared.branch)
        usage = await self._workspace_usage(task_id)
        await self.store.update_result(task_id, {"base_sha": info.base_sha, "branch": info.branch})
        await self.store.append_event(
            task_id,
            EventType.WORKSPACE_PREPARED,
            {"base_sha": info.base_sha, "branch": info.branch, **usage},
            dedup_key="workspace.prepared",
        )
        await self._save_state(task_id, 0, info.base_sha)
        return info

    def _snapshots(self) -> SnapshotStore:
        return SnapshotStore(self.settings.data_dir / "snapshots", self.settings.snapshot_max_bytes)

    def _snapshots_enabled(self, spec: CodingSpec) -> bool:
        return (self.settings.workspace_snapshots and self.settings.state_transport == "mount"
                and self.settings.sandbox_driver == "docker" and not spec.intents
                and not spec.subtasks and spec.plan == "none"
                and (spec.fork_from_snapshot or spec.labels.get("fork_from_snapshot") == "true"))

    async def _snapshot_key(self, task_id: str, commit: str) -> str:
        from sca.workspace.snapshots import preparation_key

        task = await self.store.get_task(task_id)
        spec = await self._spec(task_id)
        source = json.loads((self._workspace(task_id).task_dir / "layer-source.json").read_text())
        image = (spec.image or getattr(self._backend(spec), "sandbox_image", None)
                 or self.settings.sandbox_image)
        return preparation_key(task.tenant_id, source["repo_key"], commit,
                               await self._setup_command(task_id, spec), image)

    async def _forked_setup(self, task_id: str, sandbox_id: str) -> bool:
        task = await self.store.get_task(task_id)
        if not task.result.get("workspace_forked"):
            return False
        ws = self._workspace(task_id)
        target = ws.overlay if hasattr(ws, "volume") else ws.workspace
        marker = target.parent / f"{target.name}.snapshot"
        if not marker.is_file() or marker.read_text() != task.result.get("snapshot_id"):
            await self.store.update_result(task_id, {"workspace_forked": False})
            return False
        # Snapshot Git metadata is untrusted; only the sandbox may change its branch.
        exit_event, _ = await self._run(sandbox_id, [
            "git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "checkout", "-B", self._workspace(task_id).branch, task.result["base_sha"]],
            timeout_s=90)
        if exit_event.exit_code:
            raise ApplicationError("Snapshot branch initialization failed")
        return True

    async def _snapshot_after_setup(self, task_id: str, sandbox_id: str,
                                    setup_seconds: float) -> None:
        task = await self.store.get_task(task_id)
        spec = await self._spec(task_id)
        if not self._snapshots_enabled(spec):
            return
        metadata = await self._snapshots().publish(
            await self._snapshot_key(task_id, task.result["base_sha"]),
            self.sandbox.download(sandbox_id, "/workspace/."),
            base_commit=task.result["base_sha"],
            saved_seconds=setup_seconds + task.result.get("preparation_seconds", 0))
        await self.store.update_result(task_id, {"snapshot_id": metadata["id"],
                                                "snapshot_bytes": metadata["size_bytes"]})
        await self.store.append_event(task_id, "workspace.snapshotted", metadata,
                                      dedup_key="workspace.snapshotted")

    @activity.defn(name="coding.checkpoint_workspace")
    async def checkpoint_workspace(self, inp: StepInput) -> CheckpointResult:
        await self._ensure_state(inp.task_id)
        task = await self.store.get_task(inp.task_id)
        sandbox_id = await self._sandbox_id(inp.task_id)
        ws = self._workspace(inp.task_id)
        from sca.workspace.overlay import OverlayWorkspace

        argv = ws.commit_argv(commit_message(f"sca: turn {inp.turn}", inp.task_id))
        # Platform context (.sca/) must stay out of checkpoints even if an agent force-added it.
        # Unstage it after `add -A`: naming an ignored path in the add pathspec makes git fail.
        add = "core.fsmonitor=false add -A && "
        assert add in argv[-3]
        argv[-3] = argv[-3].replace(
            add, add + "git -c core.hooksPath=/dev/null -c core.fsmonitor=false "
            "rm -r -q --cached --ignore-unmatch -- .sca && ", 1)
        if isinstance(ws, OverlayWorkspace):
            await ws.helper(argv)
        else:
            exit_event, output = await self._run(sandbox_id, argv, timeout_s=90)
            if exit_event.exit_code != 0:
                raise ApplicationError(f"commit in sandbox failed: {tail(output, 1000)}")
        commit = await self.transport.pull_commit(sandbox_id, ws)
        await self.transport.pull_agent_home(sandbox_id, self._task_dir(inp.task_id))
        await self._save_state(inp.task_id, inp.turn, commit)
        changed = await ws.changed(task.result["base_sha"])
        await self.store.update_result(inp.task_id, {"commit": commit})
        await self.store.append_event(
            inp.task_id,
            EventType.WORKSPACE_CHECKPOINTED,
            {"turn": inp.turn, "commit": commit, "changed": changed},
            dedup_key=f"turn{inp.turn}.checkpoint",
        )
        return CheckpointResult(commit=commit, changed=changed)

    @activity.defn(name="coding.run_verification")
    async def run_verification(self, inp: StepInput) -> VerifyResult:
        task_id, turn = inp.task_id, inp.turn
        if await self._ensure_state(task_id):
            await self.sandbox.destroy(await self._sandbox_id(task_id))
            await self.restore_sandbox(StepInput(task_id, turn))
        spec = await self._spec(task_id)
        task_kind = "coding"
        with metric_guard():
            task_kind = (await self.store.get_task(task_id)).kind
        commands = spec.checks()
        if not commands:
            result = VerifyResult(passed=True, exit_code=0, output_tail="", skipped=True)
            await self.store.append_event(
                task_id,
                EventType.VERIFICATION_COMPLETED,
                {"turn": turn, "passed": True, "skipped": True},
                dedup_key=f"turn{turn}.verify",
            )
            with metric_guard():
                business_metrics.verifications.labels(task_kind, "true").inc()
            return result

        sandbox_id = await self._sandbox_id(task_id)
        await self.store.append_event(
            task_id, EventType.VERIFICATION_STARTED,
            {"turn": turn, "checks": len(commands), **self._activity_route()},
            dedup_key=f"turn{turn}.verify_started",
        )
        # All checks run at the same time; the turn is judged on all of them.
        runs = await asyncio.gather(
            *(
                self._run(sandbox_id, ["bash", "-lc", command], timeout_s=25 * 60)
                for command in commands
            )
        )
        checks = [
            {
                "command": command,
                "passed": exit_event.exit_code == 0,
                "exit_code": exit_event.exit_code,
                "timed_out": exit_event.timed_out,
            }
            for command, (exit_event, _) in zip(commands, runs, strict=True)
        ]
        many = len(commands) > 1
        log = "\n".join(
            (f"===== {command} (exit {exit_event.exit_code}) =====\n" if many else "") + output
            for command, (exit_event, output) in zip(commands, runs, strict=True)
        )
        failed = [
            (command, exit_event, output)
            for command, (exit_event, output) in zip(commands, runs, strict=True)
            if exit_event.exit_code != 0
        ]
        # What the agent is shown: only the checks that failed.
        failure_tail = "\n\n".join(
            (f"$ {command}  (exit {exit_event.exit_code})\n" if many else "")
            + (tail(output, 4000) if many else output)
            for command, exit_event, output in failed
        )
        with metric_guard():
            business_metrics.verifications.labels(task_kind, str(not failed).lower()).inc()
        name = f"verify-turn{turn}.log"
        data = log.encode("utf-8", "replace")
        uri = await self.objects.put(key_for(task_id, name), data, "text/plain; charset=utf-8")
        await self.store.add_artifact(task_id, name, "text/plain; charset=utf-8", uri, len(data))
        result = VerifyResult(
            passed=not failed,
            exit_code=failed[0][1].exit_code if failed else 0,
            output_tail=tail(failure_tail) if failed else tail(log),
        )
        await self.store.append_event(
            task_id,
            EventType.VERIFICATION_COMPLETED,
            {
                "turn": turn,
                "command": " && ".join(commands) if many else commands[0],
                "checks": checks,
                "passed": result.passed,
                "exit_code": result.exit_code,
                "timed_out": any(c["timed_out"] for c in checks),
                "output_tail": result.output_tail,
                "log_artifact": name,
            },
            dedup_key=f"turn{turn}.verify",
        )
        return result

    @activity.defn(name="coding.request_ci")
    async def request_ci(self, inp: StepInput) -> CiRequest:
        await self._ensure_state(inp.task_id)
        """Push the branch so the project's CI runs on it. The result arrives as a signal."""
        spec = await self._spec(inp.task_id)
        task = await self.store.get_task(inp.task_id)
        try:
            await self._workspace(inp.task_id).push(
                spec.repo_url, await self._repo_credential(task.tenant_id, spec.repo_url)
            )
        except _NON_RETRYABLE as e:
            raise _non_retryable(e) from e
        request = CiRequest(commit=task.result["commit"], branch=task.result["branch"])
        await self.store.append_event(
            inp.task_id,
            EventType.CI_REQUESTED,
            {"turn": inp.turn, "commit": request.commit, "branch": request.branch},
            dedup_key=f"turn{inp.turn}.ci_requested",
        )
        return request

    @activity.defn(name="coding.record_ci")
    async def record_ci(self, outcome: CiOutcome) -> None:
        await self.store.append_event(
            outcome.task_id,
            EventType.CI_COMPLETED,
            {
                "turn": outcome.turn,
                "commit": outcome.commit,
                "passed": outcome.passed,
                "summary": tail(outcome.summary or ""),
                "url": outcome.url,
                "timed_out": outcome.timed_out,
            },
            dedup_key=f"turn{outcome.turn}.ci_completed",
        )

    @activity.defn(name="coding.publish")
    async def publish(self, inp: PublishInput) -> None:
        await self._ensure_state(inp.task_id)
        task_id = inp.task_id
        spec = await self._spec(task_id)
        task = await self.store.get_task(task_id)
        ws = self._workspace(task_id)
        patch = (await ws.diff(task.result["base_sha"])).encode("utf-8", "replace")
        await self._publish_artifact(task_id, PATCH_ARTIFACT, patch, "text/x-diff")
        pushed = False
        if spec.push and (patch or task.result.get("pr")):
            await ws.push(spec.repo_url, await self._repo_credential(task.tenant_id, spec.repo_url))
            pushed = True
        await self.store.update_result(
            task_id,
            {
                "patch_artifact": PATCH_ARTIFACT,
                "patch_bytes": len(patch),
                "verification_passed": inp.verification_passed,
                "pushed": pushed,
                "turns": inp.turn,
            },
        )
        await self.store.append_event(
            task_id,
            EventType.ARTIFACT_PUBLISHED,
            {
                "name": PATCH_ARTIFACT,
                "size_bytes": len(patch),
                "pushed": pushed,
                "branch": task.result.get("branch"),
                "commit": task.result.get("commit"),
            },
            dedup_key=f"turn{inp.turn}.published",
        )
        await self.store.append_event(
            task_id,
            EventType.REVIEW_REQUESTED,
            {"turn": inp.turn, "verification_passed": inp.verification_passed},
            dedup_key=f"turn{inp.turn}.review_requested",
        )

    async def _before_agent_turn(self, task_id: str, sandbox_id: str) -> None:
        # Subtasks given by the submitter or planned automatically: hand over what did not merge.
        task = await self.store.get_task(task_id) if not hasattr(self, "name") else None
        if task is not None and task.result.get("subtasks"):
            for child in task.result.get("subtasks", []):
                if child["status"] in {"merged", "unchanged"}:
                    continue
                name = f"subtask-{child['name']}.patch"
                patch = await self.objects.get(key_for(task_id, name))
                async for ev in self.sandbox.exec(
                    sandbox_id,
                    ["bash", "-lc", 'mkdir -p .sca && cat > ".sca/$1"', "sca-patch", name],
                    workdir="/workspace",
                    stdin=patch,
                    timeout_s=30,
                ):
                    if isinstance(ev, ExecExit) and ev.exit_code:
                        raise ApplicationError(f"could not install {name} in sandbox")

    # SCM
    @activity.defn(name="coding.sync_scm")
    async def sync_scm(self, inp: ScmInput) -> None:
        from sca.scm.provider import ScmError
        from sca.scm.service import ScmService

        task = await self.store.get_task(inp.task_id)
        store = self.store.for_tenant(task.tenant_id)
        service = ScmService(self.settings, self.secrets)
        try:
            if inp.phase == "review":
                await service.open_for_review(store, task)
            elif inp.phase == "terminal":
                await service.comment_outcome(task)
            else:
                await service.record_review(store, task, inp.phase)
        except ScmError as exc:
            retryable = exc.status is None or exc.status in {405, 408, 429} or exc.status >= 500
            if retryable and activity.in_activity() and activity.info().attempt < SCM_ATTEMPTS:
                raise ApplicationError(str(exc)) from None
            await store.append_event(
                task.id, "scm.failed", {"phase": inp.phase, "message": str(exc)},
                dedup_key=f"scm.{inp.phase}.{task.result.get('turns', 0)}.failed",
            )


class ScmStateActivities(KernelActivities):
    @activity.defn(name="set_state")
    async def set_state(self, inp: SetStateInput) -> bool | None:
        await super().set_state(inp)
        if inp.state not in TERMINAL_STATES and inp.state != TaskState.AWAITING_REVIEW:
            return None
        task = await self.store.get_task(inp.task_id)
        if task.kind != "coding":
            return None
        if inp.state == TaskState.AWAITING_REVIEW:
            return True if task.spec.get("pull_request") is not None else None
        return True if "issue" in task.result else None


__all__ = [
    "CheckpointResult",
    "CiOutcome",
    "CiRequest",
    "CodingActivities",
    "PublishInput",
    "SetStateInput",
    "StepInput",
    "TurnInput",
    "TurnResult",
    "VerifyResult",
    "WorkspaceInfo",
    "branch_name",
    "sandbox_name",
]
