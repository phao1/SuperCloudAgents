"""Activities of the coding workflow: every step that does I/O (design.md §7.3)."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, dataclass
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sca.config import Settings
from sca.core.errors import AgentTurnError, ConflictError, InvalidTaskError, NotFoundError
from sca.core.interfaces import AgentBackend, ModelGateway, ObjectStore, SandboxProvider
from sca.core.models import (
    AgentContext,
    AgentEventType,
    EventType,
    ExecExit,
    Mount,
    SandboxSpec,
    TaskState,
    tail,
)
from sca.db.repo import Store
from sca.kinds.coding.spec import CodingSpec
from sca.kinds.coding.subtask_inputs import MergeInput, SubtaskInput
from sca.storage.local import key_for
from sca.workspace.cache import PIP_CACHE_ENV, RepoCache, pip_cache_mount
from sca.workspace.git import GitWorkspace

PATCH_ARTIFACT = "changes.patch"
MAX_CAPTURED_OUTPUT = 5 * 1024 * 1024
_NON_RETRYABLE = (InvalidTaskError, ConflictError, NotFoundError)

_AGENT_EVENT_TYPES = {
    AgentEventType.MESSAGE: EventType.AGENT_MESSAGE,
    AgentEventType.COMMAND: EventType.AGENT_COMMAND,
    AgentEventType.FILE_CHANGE: EventType.AGENT_FILE_CHANGE,
    AgentEventType.USAGE: EventType.AGENT_USAGE,
    AgentEventType.ERROR: EventType.AGENT_ERROR,
}


@dataclass
class SetStateInput:
    task_id: str
    state: str
    reason: str | None = None


@dataclass
class WorkspaceInfo:
    base_sha: str
    branch: str


@dataclass
class TurnInput:
    task_id: str
    turn: int
    prompt: str
    session_id: str | None = None


@dataclass
class TurnResult:
    session_id: str | None
    final_message: str


@dataclass
class StepInput:
    task_id: str
    turn: int


@dataclass
class CheckpointResult:
    commit: str
    changed: bool


@dataclass
class VerifyResult:
    passed: bool
    exit_code: int
    output_tail: str
    skipped: bool = False


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


@dataclass
class PublishInput:
    task_id: str
    turn: int
    verification_passed: bool


def _non_retryable(exc: Exception) -> ApplicationError:
    return ApplicationError(str(exc), type=type(exc).__name__, non_retryable=True)


def branch_name(task_id: str) -> str:
    return f"sca/{task_id[:8]}"


def sandbox_name(task_id: str) -> str:
    return f"task-{task_id}"


class CodingActivities:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        sandbox: SandboxProvider,
        agent: AgentBackend,
        objects: ObjectStore,
        gateway: ModelGateway | None = None,
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.store = store
        self.sandbox = sandbox
        self.agent = agent
        self.objects = objects

    def all(self) -> list:
        return [
            self.prepare_mirror,
            self.subtask_step,
            self.merge_subtasks,
            self.build_merged_workspace,
            self.load_task,
            self.set_state,
            self.prepare_workspace,
            self.create_sandbox,
            self.release_sandbox,
            self.restore_sandbox,
            self.run_setup,
            self.run_agent_turn,
            self.checkpoint_workspace,
            self.run_verification,
            self.publish,
            self.destroy_sandbox,
            self.request_ci,
            self.record_ci,
            self.check_budget,
        ]

    # ------------------------------------------------------------------ helpers

    async def _spec(self, task_id: str) -> CodingSpec:
        task = await self.store.get_task(task_id)
        return CodingSpec.model_validate(task.spec)

    def _task_dir(self, task_id: str) -> Path:
        return self.settings.task_dir(task_id)

    def _workspace(self, task_id: str) -> GitWorkspace:
        cache_root = self.settings.data_dir.resolve() / "cache" / "repos"
        cache = RepoCache(cache_root) if self.settings.repo_cache else None
        return GitWorkspace(self._task_dir(task_id), branch_name(task_id), cache=cache)

    def _agent_ctx(self, task_id: str, spec: CodingSpec) -> AgentContext:
        return AgentContext(
            task_id=task_id,
            agent_home_host=str(self._task_dir(task_id) / "agent_home"),
            model=spec.model or self.settings.default_model,
        )

    async def _sandbox_id(self, task_id: str) -> str:
        task = await self.store.get_task(task_id)
        sandbox_id = task.result.get("sandbox_id")
        if not sandbox_id:
            raise ApplicationError("task has no sandbox", non_retryable=True)
        return sandbox_id

    async def _run(
        self, sandbox_id: str, argv: list[str], timeout_s: float
    ) -> tuple[ExecExit, str]:
        """Run a command in the sandbox and capture its combined output."""
        chunks: list[str] = []
        size = 0
        exit_event = ExecExit(exit_code=-1)
        async for ev in self.sandbox.exec(
            sandbox_id, argv, workdir="/workspace", timeout_s=timeout_s
        ):
            if isinstance(ev, ExecExit):
                exit_event = ev
            else:
                activity.heartbeat()
                if size < MAX_CAPTURED_OUTPUT:
                    chunks.append(ev.text)
                    size += len(ev.text) + 1
        return exit_event, "\n".join(chunks)

    # --------------------------------------------------------------- activities

    @activity.defn
    async def prepare_mirror(self, task_id: str) -> WorkspaceInfo:
        spec = await self._spec(task_id)
        ws = self._workspace(task_id)
        task = await self.store.get_task(task_id)
        if task.result.get("base_sha") and ws.mirror.exists():
            info = WorkspaceInfo(task.result["base_sha"], ws.branch)
        else:
            prepared = await ws.prepare(spec.repo_url, spec.base_ref, mirror_only=True)
            info = WorkspaceInfo(prepared.base_sha, prepared.branch)
            await self.store.update_result(task_id, asdict(info))
        await self.store.append_event(
            task_id,
            EventType.WORKSPACE_PREPARED,
            {**asdict(info), "mirror_only": True},
            dedup_key="workspace.prepared",
        )
        return info

    @activity.defn
    async def subtask_step(self, inp: SubtaskInput) -> dict:
        from sca.kinds.coding.subtasks import ChildActivities

        child = ChildActivities(self, inp.name)
        if inp.action == "prepare":
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
            await child.create_sandbox(inp.task_id)
            return {}
        if inp.action in {"run_setup", "destroy_sandbox"}:
            await getattr(child, inp.action)(inp.task_id)
            return {}
        if inp.action == "run_agent_turn":
            result = await child.run_agent_turn(
                TurnInput(inp.task_id, inp.turn, inp.prompt, inp.session_id)
            )
        else:
            result = await getattr(child, inp.action)(StepInput(inp.task_id, inp.turn))
        if inp.action == "check_budget":
            return {"reason": result}
        return asdict(result)

    @activity.defn
    async def merge_subtasks(self, inp: MergeInput) -> list[dict]:
        from sca.workspace.merge import merge_subtasks

        task = await self.store.get_task(inp.task_id)
        ws = self._workspace(inp.task_id)
        commit, results = await merge_subtasks(
            ws.mirror,
            task.result["base_sha"],
            ws.branch,
            [(self._task_dir(inp.task_id) / "sub" / r.name / "mirror.git", r) for r in inp.results],
        )
        for r in results:
            if r.status not in {"merged", "unchanged"}:
                child = GitWorkspace(self._task_dir(inp.task_id) / "sub" / r.name, r.branch)
                patch = (await child.diff(task.result["base_sha"]) if r.commit else "").encode()
                name = f"subtask-{r.name}.patch"
                uri = await self.objects.put(key_for(inp.task_id, name), patch, "text/x-diff")
                await self.store.add_artifact(inp.task_id, name, "text/x-diff", uri, len(patch))
        statuses = [asdict(r) for r in results]
        await self.store.update_result(inp.task_id, {"commit": commit, "subtasks": statuses})
        await self.store.append_event(
            inp.task_id,
            EventType.SUBTASKS_MERGED,
            {"subtasks": statuses},
            dedup_key="subtasks.merged",
        )
        return statuses

    @activity.defn
    async def build_merged_workspace(self, task_id: str) -> None:
        task = await self.store.get_task(task_id)
        ws = self._workspace(task_id)
        await ws.build_workspace(task.result["commit"])

    @activity.defn
    async def load_task(self, task_id: str) -> dict:
        try:
            spec = (await self._spec(task_id)).model_dump()
            # Platform settings the workflow needs travel with the spec.
            return {
                **spec,
                "review_idle_release_s": self.settings.review_idle_release_s,
                "turn_heartbeat_timeout_s": self.settings.turn_heartbeat_timeout_s,
            }
        except _NON_RETRYABLE as e:
            raise _non_retryable(e) from e

    @activity.defn
    async def set_state(self, inp: SetStateInput) -> None:
        try:
            await self.store.set_state(inp.task_id, TaskState(inp.state), inp.reason)
        except _NON_RETRYABLE as e:
            raise _non_retryable(e) from e

    @activity.defn
    async def prepare_workspace(self, task_id: str) -> WorkspaceInfo:
        spec = await self._spec(task_id)
        try:
            prepared = await self._workspace(task_id).prepare(spec.repo_url, spec.base_ref)
        except InvalidTaskError as e:
            raise _non_retryable(e) from e
        info = WorkspaceInfo(base_sha=prepared.base_sha, branch=prepared.branch)
        await self.store.update_result(task_id, {"base_sha": info.base_sha, "branch": info.branch})
        await self.store.append_event(
            task_id,
            EventType.WORKSPACE_PREPARED,
            {"base_sha": info.base_sha, "branch": info.branch},
            dedup_key="workspace.prepared",
        )
        return info

    @activity.defn
    async def create_sandbox(self, task_id: str) -> str:
        sandbox_id, image = await self._create_sandbox(task_id)
        await self.store.append_event(
            task_id,
            EventType.SANDBOX_CREATED,
            {"sandbox_id": sandbox_id, "image": image},
            dedup_key="sandbox.created",
        )
        return sandbox_id

    @activity.defn
    async def release_sandbox(self, inp: StepInput) -> None:
        """Free the sandbox of an idle task. Workspace and session history stay on disk."""
        sandbox_id = await self._sandbox_id(inp.task_id)
        await self.sandbox.destroy(sandbox_id)
        await self.store.append_event(
            inp.task_id,
            EventType.SANDBOX_RELEASED,
            {"sandbox_id": sandbox_id, "after_turn": inp.turn},
            dedup_key=f"turn{inp.turn}.sandbox_released",
        )

    @activity.defn
    async def restore_sandbox(self, inp: StepInput) -> None:
        """Bring back a released sandbox on the same workspace and session history."""
        spec = await self._spec(inp.task_id)
        sandbox_id, _ = await self._create_sandbox(inp.task_id)
        if spec.setup_command:
            # Whatever setup installed outside the mounted directories went with the old sandbox.
            exit_event, output = await self._run(
                sandbox_id, ["bash", "-lc", spec.setup_command], timeout_s=14 * 60
            )
            if exit_event.exit_code != 0:
                raise ApplicationError(
                    f"setup command failed on restore: {tail(output, 1000)}",
                    type="SetupFailed",
                    non_retryable=True,
                )
        await self.store.append_event(
            inp.task_id,
            EventType.SANDBOX_RESTORED,
            {"sandbox_id": sandbox_id, "after_turn": inp.turn},
            dedup_key=f"turn{inp.turn}.sandbox_restored",
        )

    async def _create_sandbox(self, task_id: str) -> tuple[str, str]:
        spec = await self._spec(task_id)
        additions = self.agent.sandbox_additions(self._agent_ctx(task_id, spec))
        cache_mounts, cache_env = [], {}
        if self.settings.pip_cache:
            # Per tenant: a shared writable cache would let one tenant plant a package that
            # another tenant installs.
            tenant = (await self.store.get_task(task_id)).tenant_id
            cache_root = self.settings.data_dir.resolve() / "cache"
            cache_mounts, cache_env = [pip_cache_mount(cache_root, tenant)], PIP_CACHE_ENV
        sandbox_spec = SandboxSpec(
            image=spec.image or self.settings.sandbox_image,
            mounts=[
                Mount(source=str(self._task_dir(task_id) / "workspace"), target="/workspace"),
                *additions.mounts,
                *cache_mounts,
            ],
            env={**cache_env, **additions.env},
            cpus=self.settings.sandbox_cpus,
            memory_mb=self.settings.sandbox_memory_mb,
            network=self.settings.sandbox_network,
            runtime=self.settings.sandbox_runtime,
            labels={"sca.task_id": task_id},
        )
        sandbox_id = await self.sandbox.create(sandbox_name(task_id), sandbox_spec)
        await self.store.update_result(task_id, {"sandbox_id": sandbox_id})
        return sandbox_id, sandbox_spec.image

    @activity.defn
    async def run_setup(self, task_id: str) -> None:
        spec = await self._spec(task_id)
        sandbox_id = await self._sandbox_id(task_id)
        exit_event, output = await self._run(
            sandbox_id, ["bash", "-lc", spec.setup_command or "true"], timeout_s=14 * 60
        )
        await self.store.append_event(
            task_id,
            EventType.SETUP_COMPLETED,
            {
                "command": spec.setup_command,
                "exit_code": exit_event.exit_code,
                "timed_out": exit_event.timed_out,
                "output_tail": tail(output),
            },
        )
        if exit_event.exit_code != 0:
            raise ApplicationError(
                f"setup command failed with exit status {exit_event.exit_code}: {tail(output, 1000)}",
                type="SetupFailed",
                non_retryable=True,
            )

    @activity.defn
    async def run_agent_turn(self, inp: TurnInput) -> TurnResult:
        task_id, turn = inp.task_id, inp.turn
        attempt = activity.info().attempt
        key = f"turn{turn}.attempt{attempt}"
        spec = await self._spec(task_id)
        sandbox_id = await self._sandbox_id(task_id)
        if not hasattr(self, "name") and spec.subtasks:
            task = await self.store.get_task(task_id)
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
        # On a retried attempt the session may already exist from the failed attempt.
        session_id = inp.session_id or await self.store.get_session_id(task_id)

        await self.store.append_event(
            task_id,
            EventType.AGENT_TURN_STARTED,
            {"turn": turn, "attempt": attempt, "prompt": inp.prompt, "backend": self.agent.name},
            dedup_key=f"{key}.started",
        )

        async def beat() -> None:
            # The model can think for a long time without producing output.
            while True:
                await asyncio.sleep(min(20, self.settings.turn_heartbeat_timeout_s / 4))
                activity.heartbeat()

        ctx = self._agent_ctx(task_id, spec)
        key_alias = f"task-{task_id}-turn{turn}-attempt{attempt}"
        if hasattr(self, "name"):
            key_alias += f"-{self.name}"
        if self.gateway:
            # The credential lives exactly as long as the turn and never enters the
            # workflow history.
            ctx.model_base_url = self.gateway.sandbox_url
            ctx.model_api_key = await self.gateway.issue_key(
                key_alias,
                models=[ctx.model or self.settings.default_model],
                ttl_s=spec.turn_timeout_s + 120,
                max_budget_usd=self.settings.model_gateway_turn_budget_usd,
                metadata={"task_id": task_id, "turn": turn},
            )

        beater = asyncio.create_task(beat())
        final_message = ""
        n = 0
        try:
            async for ev in self.agent.run_turn(
                self.sandbox,
                sandbox_id,
                ctx,
                inp.prompt,
                session_id=session_id,
                timeout_s=spec.turn_timeout_s,
            ):
                activity.heartbeat()
                n += 1
                if ev.type == AgentEventType.SESSION_STARTED:
                    session_id = ev.data.get("session_id") or session_id
                    if session_id:
                        await self.store.set_session(task_id, self.agent.name, session_id)
                    continue
                if ev.type == AgentEventType.TURN_COMPLETED:
                    final_message = ev.data.get("final_message", "")
                    continue
                if ev.type == AgentEventType.USAGE:
                    await self.store.add_usage(
                        task_id, {k: v for k, v in ev.data.items() if isinstance(v, int)}
                    )
                await self.store.append_event(
                    task_id,
                    _AGENT_EVENT_TYPES[ev.type],
                    {"turn": turn, **ev.data},
                    dedup_key=f"{key}.{n}",
                )
        except AgentTurnError as e:
            await self.store.append_event(
                task_id,
                EventType.AGENT_ERROR,
                {"turn": turn, "attempt": attempt, "message": str(e)},
                dedup_key=f"{key}.failed",
            )
            raise
        finally:
            beater.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beater
            if self.gateway:
                spend = await asyncio.shield(self.gateway.revoke_key(key_alias))
                if spend:
                    await self.store.add_usage(task_id, {"cost_micro_usd": round(spend * 1e6)})

        await self.store.append_event(
            task_id,
            EventType.AGENT_TURN_COMPLETED,
            {"turn": turn, "final_message": final_message},
            dedup_key=f"{key}.completed",
        )
        return TurnResult(session_id=session_id, final_message=final_message)

    @activity.defn
    async def checkpoint_workspace(self, inp: StepInput) -> CheckpointResult:
        task = await self.store.get_task(inp.task_id)
        sandbox_id = await self._sandbox_id(inp.task_id)
        ws = self._workspace(inp.task_id)
        exit_event, output = await self._run(
            sandbox_id, ws.commit_argv(f"sca: turn {inp.turn}"), timeout_s=90
        )
        if exit_event.exit_code != 0:
            raise ApplicationError(f"commit in sandbox failed: {tail(output, 1000)}")
        commit = await ws.fetch()
        changed = await ws.changed(task.result["base_sha"])
        await self.store.update_result(inp.task_id, {"commit": commit})
        await self.store.append_event(
            inp.task_id,
            EventType.WORKSPACE_CHECKPOINTED,
            {"turn": inp.turn, "commit": commit, "changed": changed},
            dedup_key=f"turn{inp.turn}.checkpoint",
        )
        return CheckpointResult(commit=commit, changed=changed)

    @activity.defn
    async def run_verification(self, inp: StepInput) -> VerifyResult:
        task_id, turn = inp.task_id, inp.turn
        spec = await self._spec(task_id)
        commands = spec.checks()
        if not commands:
            result = VerifyResult(passed=True, exit_code=0, output_tail="", skipped=True)
            await self.store.append_event(
                task_id,
                EventType.VERIFICATION_COMPLETED,
                {"turn": turn, "passed": True, "skipped": True},
                dedup_key=f"turn{turn}.verify",
            )
            return result

        sandbox_id = await self._sandbox_id(task_id)
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

    @activity.defn
    async def request_ci(self, inp: StepInput) -> CiRequest:
        """Push the branch so the project's CI runs on it. The result arrives as a signal."""
        spec = await self._spec(inp.task_id)
        task = await self.store.get_task(inp.task_id)
        try:
            await self._workspace(inp.task_id).push(spec.repo_url)
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

    @activity.defn
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

    @activity.defn
    async def check_budget(self, inp: StepInput) -> str | None:
        """Why the task must stop spending, or None."""
        reason = await self.store.check_budget(inp.task_id)
        if reason:
            await self.store.append_event(
                inp.task_id,
                EventType.BUDGET_EXCEEDED,
                {"turn": inp.turn, "reason": reason},
                dedup_key=f"turn{inp.turn}.budget",
            )
        return reason

    @activity.defn
    async def publish(self, inp: PublishInput) -> None:
        task_id = inp.task_id
        spec = await self._spec(task_id)
        task = await self.store.get_task(task_id)
        ws = self._workspace(task_id)
        patch = (await ws.diff(task.result["base_sha"])).encode("utf-8", "replace")
        uri = await self.objects.put(key_for(task_id, PATCH_ARTIFACT), patch, "text/x-diff")
        await self.store.add_artifact(task_id, PATCH_ARTIFACT, "text/x-diff", uri, len(patch))
        pushed = False
        if spec.push and patch:
            await ws.push(spec.repo_url)
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

    @activity.defn
    async def destroy_sandbox(self, task_id: str) -> None:
        sandbox_id = f"sca-{sandbox_name(task_id)}"
        with contextlib.suppress(NotFoundError):
            sandbox_id = (await self.store.get_task(task_id)).result.get("sandbox_id", sandbox_id)
        await self.sandbox.destroy(sandbox_id)
        await self.store.append_event(
            task_id,
            EventType.SANDBOX_DESTROYED,
            {"sandbox_id": sandbox_id},
            dedup_key="sandbox.destroyed",
        )
