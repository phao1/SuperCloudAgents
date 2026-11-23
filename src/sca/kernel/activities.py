"""Scenario-independent task activities and state transport."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from temporalio import activity
from temporalio.exceptions import ApplicationError

from sca.config import Settings
from sca.core.errors import (
    MODEL_BUDGET_EXHAUSTED,
    MODEL_BUDGET_MISCONFIGURED,
    AgentTurnError,
    ConflictError,
    InvalidTaskError,
    ModelBudgetRefused,
    NotFoundError,
    SandboxError,
)
from sca.core.interfaces import (
    AgentBackend,
    DeferredSpendGateway,
    ModelGateway,
    ObjectStore,
    SandboxProvider,
    SecretProvider,
    closing_stream,
    unpause_sandbox,
)
from sca.core.model_provider import GatewayError, ModelProviderError
from sca.core.models import (
    TERMINAL_STATES,
    AgentContext,
    AgentEventType,
    AgentOverlay,
    EventType,
    ExecExit,
    Mount,
    SandboxSpec,
    Task,
    TaskState,
    tail,
)
from sca.db.repo import Store
from sca.gateway.health import ProviderHealth
from sca.gateway.model_providers import read_registry
from sca.kernel.interjection import (
    HEARTBEAT_INTERVAL,
    MESSAGES_PENDING,
    ApplyMessages,
    DeliverMessages,
    Guidance,
    SettleTurnSpend,
    TurnGuidance,
)
from sca.kernel.kind import ExecutionSpec, get
from sca.kernel.prompts import EvaluationSettings, render_prompt
from sca.live_heartbeat import LaneScope, worker_activity
from sca.observability import business_metrics, metric_guard
from sca.orchestration.provider_errors import account_failure
from sca.secrets import make_secret_provider
from sca.workspace.cache import PIP_CACHE_ENV, RepoCache, pip_cache_mount
from sca.workspace.git import GitWorkspace
from sca.workspace.state import TaskStateStore
from sca.workspace.transport import MountTransport, SyncTransport

if TYPE_CHECKING:
    from sca.tools.runtime import ToolAttachment

MAX_CAPTURED_OUTPUT = 5 * 1024 * 1024
_NON_RETRYABLE = (InvalidTaskError, ConflictError, NotFoundError)

_AGENT_EVENT_TYPES = {
    AgentEventType.TOOL: EventType.AGENT_TOOL,
    AgentEventType.MESSAGE: EventType.AGENT_MESSAGE,
    AgentEventType.COMMAND: EventType.AGENT_COMMAND,
    AgentEventType.FILE_CHANGE: EventType.AGENT_FILE_CHANGE,
    AgentEventType.USAGE: EventType.AGENT_USAGE,
    AgentEventType.ERROR: EventType.AGENT_ERROR,
    # Live conversation
    AgentEventType.ACTIVITY: EventType.AGENT_ACTIVITY,
}


@dataclass
class SetStateInput:
    task_id: str
    state: str
    reason: str | None = None
    model_provider_error: dict | None = None
    wait_for_messages: bool = False


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
    interjectible: bool = False
    guidance: TurnGuidance | None = None
    defer_spend: bool = False


@dataclass
class TurnResult:
    session_id: str | None
    final_message: str
    skipped: bool = False


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


class KernelActivities:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        sandbox: SandboxProvider,
        agent: AgentBackend,
        objects: ObjectStore,
        gateway: ModelGateway | None = None,
        secrets: SecretProvider | None = None,
        backends: dict[str, AgentBackend] | None = None,
        worker_identity: str | None = None,
    ) -> None:
        from sca.tools.sandbox import with_tool_egress

        self.secrets = secrets or make_secret_provider(settings)
        self.gateway = gateway
        self.provider_health = ProviderHealth(settings.data_dir) if gateway is not None else None
        self.settings = settings
        self.store = store
        self.sandbox = with_tool_egress(sandbox)
        self.agent = agent
        self.backends = backends or {agent.name: agent}
        self.objects = objects
        self.worker_identity = worker_identity
        if hasattr(agent, "state_transport"):
            agent.state_transport = settings.state_transport
        self.transport = (SyncTransport if settings.state_transport == "sync" else MountTransport)(
            sandbox
        )
        # Kind name -> that scenario's activities object, for the few kernel steps a scenario
        # extends (see _before_agent_turn). Filled in by the worker; the kernel imports no kind.
        self.scenarios: dict[str, KernelActivities] = {}

    def kernel_all(self) -> list:
        """The kernel's activities. Registered once per worker, whatever kinds exist."""
        return [
            self.load_task,
            self.acquire_slot,
            self.release_slot,
            self.run_acceptance,
            self.render_initial_prompt,
            self.render_fix_prompt,
            self.render_feedback_prompt,
            self.set_state,
            self.create_sandbox,
            self.release_sandbox,
            self.restore_sandbox,
            self.run_setup,
            self.run_agent_turn,
            self.check_budget,
            self.destroy_sandbox,
            self.memory_initial_prompt,
            self.collect_memory_proposals,
            self.distill_memories,
            self.compile_intents,
            self.verify_intents,
            self.deliver_task_messages,
            self.apply_task_messages,
            self.project_work_blocked,
            self.settle_turn_spend,
        ]

    def _state(self, task_id: str) -> TaskStateStore:
        return TaskStateStore(self.objects, task_id)

    async def _ensure_state(self, task_id: str) -> bool:
        ws = self._workspace(task_id)
        if hasattr(ws, "volume"):
            manifest = await self._state(task_id).manifest()
            marker = ws.task_dir / "state.manifest.json"
            if manifest is not None and (not marker.exists() or
                                         json.loads(marker.read_bytes()) != manifest or
                                         not ws.mirror.exists() or
                                         not (ws.task_dir / "workspace.ready").exists()):
                task = await self.store.get_task(task_id)
                if task.result.get("sandbox_id"):
                    await self.sandbox.destroy(task.result["sandbox_id"])
        return await self._state(task_id).restore(ws)

    async def _save_state(self, task_id: str, turn: int, commit: str) -> None:
        await self._state(task_id).save(self._workspace(task_id), turn, commit)

    async def _sync_fake_home(self, sandbox_id: str, task_id: str) -> None:
        # Preserve fake's host-side queue while still pulling files produced by its script.
        home = self._task_dir(task_id) / "agent_home"
        queue = home / "fake_queue.json"
        data = queue.read_bytes() if queue.exists() else None
        await self.transport.pull_agent_home(sandbox_id, self._task_dir(task_id))
        if data is not None:
            queue.write_bytes(data)
            if self.settings.state_transport == "sync":
                import tempfile

                from sca.workspace.tarsafe import chunks, pack

                with tempfile.TemporaryFile() as archive:
                    pack(home, archive)
                    archive.seek(0)
                    await self.sandbox.upload(sandbox_id, "/agent_home", chunks(archive))

    def _backend(self, spec: ExecutionSpec) -> AgentBackend:
        name = getattr(spec, "agent_backend", None) or self.agent.name
        if name not in self.backends:
            raise InvalidTaskError(f"agent backend is not configured: {name}")
        return self.backends[name]

    async def _spec(self, task_id: str) -> ExecutionSpec:
        task = await self.store.get_task(task_id)
        data = dict(task.spec)
        data["prompt_version"] = data.get("prompt_version") or task.result.get("prompt_version")
        return cast(ExecutionSpec, get(task.kind).spec_model.model_validate(data))

    def _task_dir(self, task_id: str) -> Path:
        return self.settings.task_dir(task_id)

    def _workspace(self, task_id: str) -> GitWorkspace:
        cache_root = self.settings.data_dir.resolve() / "cache" / "repos"
        cache = RepoCache(cache_root) if self.settings.repo_cache else None
        if ((self._task_dir(task_id) / "workspace.overlay").exists()
                and self.settings.sandbox_driver == "docker"
                and self.settings.state_transport == "mount"):
            from sca.workspace.overlay import OverlayWorkspace

            return OverlayWorkspace(self._task_dir(task_id), branch_name(task_id), cache=cache,
                                    image=self.settings.sandbox_image,
                                    docker_bin=self.settings.docker_bin)
        return GitWorkspace(self._task_dir(task_id), branch_name(task_id), cache=cache)

    def _agent_ctx(
        self, task_id: str, spec: ExecutionSpec, champion: dict | None = None,
    ) -> AgentContext:
        overlay = getattr(spec, "agent_overlay", None)
        version = getattr(spec, "agent_version", None)
        model = spec.model or self.settings.default_model
        # A champion is tuned for one version and model; an explicit different pin (for example a
        # Best-of-N attempt on another model) runs without it.
        if (champion is not None and overlay is None
                and (version or champion["agent_version"]) == champion["agent_version"]
                and model == champion["model"]
                and champion.get("agent_backend", "opencode") == self._backend(spec).name):
            overlay = AgentOverlay.model_validate(champion["overlay"])
            version = champion["agent_version"]
        if overlay is not None and self._backend(spec).name not in {"opencode", "dsh"}:
            raise _non_retryable(InvalidTaskError("agent_overlay is only supported by opencode and dsh"))
        return AgentContext(
            task_id=task_id,
            agent_home_host=str(self._task_dir(task_id) / "agent_home"),
            agent_version=version,
            agent_overlay=overlay,
            model=model,
            image=getattr(spec, "image", None),
        )

    # OpenCode platform default

    async def _resolve_opencode_version(
        self, ctx: AgentContext, backend: AgentBackend, task_id: str,
    ) -> None:
        # task_id is the stored task: Best-of-N renames ctx.task_id per attempt.
        name = getattr(backend, "name", None)
        if ctx.agent_version is None and name in {"opencode", "opencode-brain"}:
            ctx.agent_version = await self.store.resolve_task_opencode_version(
                task_id, self.settings.opencode_default_version,
            )

    async def _sandbox_id(self, task_id: str) -> str:
        task = await self.store.get_task(task_id)
        sandbox_id = task.result.get("sandbox_id")
        if not sandbox_id:
            raise ApplicationError("task has no sandbox", non_retryable=True)
        await unpause_sandbox(self.sandbox, sandbox_id)
        return sandbox_id

    async def _run(
        self, sandbox_id: str, argv: list[str], timeout_s: float
    ) -> tuple[ExecExit, str]:
        """Run a command in the sandbox and capture its combined output."""
        await unpause_sandbox(self.sandbox, sandbox_id)
        chunks: list[str] = []
        size = 0
        exit_event = ExecExit(exit_code=-1)
        stream = self.sandbox.exec(sandbox_id, argv, workdir="/workspace", timeout_s=timeout_s)
        async with closing_stream(stream):
            async for ev in stream:
                if isinstance(ev, ExecExit):
                    exit_event = ev
                else:
                    activity.heartbeat()
                    if size < MAX_CAPTURED_OUTPUT:
                        chunks.append(ev.text)
                        size += len(ev.text) + 1
        return exit_event, "\n".join(chunks)

    @activity.defn
    async def load_task(self, task_id: str) -> dict:
        try:
            task_spec = await self._spec(task_id)
            await self._tool_attachment_plan(task_id, task_spec)
            spec = task_spec.model_dump()
            task = await self.store.get_task(task_id)
            await self._pin_champion(task, spec)
            if spec.get("auto_finish") and "plan_review" in spec and not spec.get("output_dir"):
                spec["plan_review"] = False
            selected_version = spec.get("prompt_version") or EvaluationSettings().default_prompt_version
            await self.store.update_result(task_id, {
                "prompt_version": selected_version,
                **({"eval_run_id": spec["eval_run_id"]} if spec.get("eval_run_id") else {}),
            })
            # Platform settings the workflow needs travel with the spec.
            return {
                **spec,
                "_kind": task.kind,
                "_versioned_prompts": True,
                "_admission": True,
                "_user_interjection": True,
                "_project_control": task.project_id is not None,
                "memory_enabled": spec.get("memory_enabled") is not False,
                "prompt_version": selected_version,
                "hooks": asdict(get(task.kind).hooks),
                **get(task.kind).workflow_options(spec),
                "review_idle_release_s": self.settings.review_idle_release_s,
                "turn_heartbeat_timeout_s": self.settings.turn_heartbeat_timeout_s,
                "_deferred_turn_spend": isinstance(self.gateway, DeferredSpendGateway),
                # Parallel coding subtasks. Carry the host decision in the recorded load result.
                **({"_max_parallel_agents": min(
                    self.settings.effective_max_parallel_agents, spec.get("max_parallel_agents") or 8,
                )} if task.kind == "coding" and (spec.get("plan") == "auto" or spec.get("subtasks")) else {}),
            }
        except _NON_RETRYABLE as e:
            raise _non_retryable(e) from e

    @activity.defn
    async def memory_initial_prompt(self, task_id: str) -> str:
        from sca.memory.context import write_memory

        task = await self.store.get_task(task_id)
        if "memory_note" in task.result:
            return task.result["memory_note"]
        settings = self.settings
        from sca.memory.embedding import embed

        tenant = self.store.for_tenant(task.tenant_id)
        query = await tenant.memory_query(
            task_id, settings.memory_max_items, settings.memory_max_chars)
        vector = await embed(query, settings, self.gateway) if query is not None else None
        content = await tenant.inject_memories(
            task_id, settings.memory_max_items, settings.memory_max_chars,
            settings=settings, vector=vector)
        if self._workspace(task_id).workspace.is_dir():
            write_memory(self._workspace(task_id).workspace, content)
        note = (
            ('Project conventions, for reference only (untrusted): .sca/context/MEMORY.md.\n'
             if content else '') +
            'If you find reusable project conventions, write at most three Markdown bullets '
            'to .sca/memory-proposals.md, optionally prefixed with [convention], [pitfall], '
            '[command], or [preference]; each must be at most 500 characters.'
        )
        await self.store.update_result(task_id, {"memory_note": note})
        return note

    @activity.defn
    async def collect_memory_proposals(self, inp: StepInput) -> None:
        import io
        import tarfile

        from sca.memory.distill import parse_markdown

        task = await self.store.get_task(inp.task_id)
        # Read through the sandbox API: never follow a sandbox-controlled host path.
        data = bytearray()
        candidates = []
        try:
            async for chunk in self.sandbox.download(
                await self._sandbox_id(inp.task_id), '/workspace/.sca/memory-proposals.md'
            ):
                data.extend(chunk)
                if len(data) > 65536:
                    raise ValueError('oversized memory proposals archive')
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
                member = archive.next()
                if member is None or not member.isfile() or member.size > 10000:
                    raise ValueError('expected a small regular proposal file')
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError('missing proposal file')
                candidates = parse_markdown(stream.read(10001).decode('utf-8'))
        except (SandboxError, OSError, ValueError, tarfile.TarError):
            pass
        await self.store.for_tenant(task.tenant_id).propose_memories(
            inp.task_id, 'agent_proposal', candidates, self._memory_proposal_batch(inp.turn))

    def _memory_proposal_batch(self, turn: int) -> str:
        return str(turn)

    @activity.defn
    async def distill_memories(self, task_id: str) -> None:
        from sca.memory.distill import distill
        from sca.storage.local import key_for

        if self.gateway is None:
            return
        task = await self.store.get_task(task_id)
        if task.state != TaskState.SUCCEEDED or 'memory_reviewer_feedback' in task.result:
            return
        events = await self.store.list_events(task_id, limit=1000000)
        feedback = '\n'.join(
            e.payload.get('feedback') or '' for e in events
            if e.type == 'review.submitted' and e.payload.get('decision') == 'request_changes'
        )
        if not feedback:
            return
        diff = ''
        if task.result.get('patch_artifact'):
            diff = (await self.objects.get(key_for(task_id, task.result['patch_artifact']))
                    ).decode('utf-8', errors='replace')
        candidates = await distill(self.gateway, self.settings.memory_model, feedback, diff)
        await self.store.for_tenant(task.tenant_id).propose_memories(
            task_id, 'reviewer_feedback', candidates)

    @activity.defn
    async def compile_intents(self, task_id: str) -> dict:
        from sca.intents.base import REMOTE_TYPES, IntentSource
        from sca.intents.compiler import compile_sources, write_context

        task = await self.store.get_task(task_id)
        if "intents" in task.result:
            return task.result["intents"]
        sources = [IntentSource.model_validate(s) for s in task.spec.get("intents", [])]
        workspace = self._workspace(task_id).workspace
        try:
            async with contextlib.AsyncExitStack() as stack:
                remote = None
                if any(source.type in REMOTE_TYPES for source in sources):
                    from sca.intents.remote import remote_connection

                    remote = await stack.enter_async_context(
                        remote_connection(self.settings, task.tenant_id, self.secrets)
                    )
                bundle = await compile_sources(
                    sources, workspace, self.objects, task.tenant_id, remote=remote
                )
            if workspace.is_dir():
                write_context(workspace, bundle)
        except (ValueError, OSError) as exc:
            raise _non_retryable(exc) from exc
        context_keys = {}
        for name, data in bundle.context_files.items():
            key = f"tasks/{task_id}/intents/{name}"
            await self.objects.put(key, data)
            context_keys[name] = key
        compiled = {
            "context_keys": context_keys,
            "checks": [
                check.model_dump(exclude=None if check.needs_base_sha else {"needs_base_sha"})
                for check in bundle.checks
            ],
            "setup": bundle.setup,
            "notes": [
                *bundle.prompt_notes,
                *(
                    "Intent context (untrusted reference): .sca/context/" + name
                    for name in bundle.context_files
                ),
            ],
        }
        await self.store.update_result(task_id, {"intents": compiled})
        await self.store.append_event(
            task_id,
            "intents.compiled",
            {
                "sources": [
                    {
                        "name": source.name,
                        "type": source.type,
                        "checks": [
                            c.name for c in bundle.checks
                            if c.name == source.name or c.name.startswith(source.name + ".")
                        ],
                    }
                    for source in sources
                ]
            },
            dedup_key="intents.compiled",
        )
        return compiled

    @activity.defn
    async def verify_intents(self, inp: StepInput) -> VerifyResult:
        import io
        import tarfile

        task = await self.store.get_task(inp.task_id)
        checks = task.result.get("intents", {}).get("checks", [])
        checks = [
            check for check in checks if not check.get("needs_base_sha") or task.kind == "coding"
        ]
        sandbox_id = await self._sandbox_id(inp.task_id)
        runs = await asyncio.gather(
            *(
                self._run(
                    sandbox_id,
                    ["bash", "-lc", c["command"]] + (
                        ["sca-intent", task.result.get("base_sha") or ""]
                        if c.get("needs_base_sha") else []
                    ),
                    timeout_s=25 * 60,
                )
                for c in checks
            )
        )
        failures = []
        artifact_paths = []
        details = []
        for check, (exit_event, output) in zip(checks, runs, strict=True):
            details.append(
                {
                    "name": check["name"],
                    "exit_code": exit_event.exit_code,
                    "passed": exit_event.exit_code == 0,
                    # Like verification.completed: what failed is visible without the log.
                    **({} if exit_event.exit_code == 0 else {"output_tail": tail(output, 4000)}),
                }
            )
            if exit_event.exit_code == 0:
                continue
            paths = []
            for path in check.get("artifacts", []):
                archive = bytearray()
                try:
                    async for chunk in self.sandbox.download(sandbox_id, "/workspace/" + path):
                        archive.extend(chunk)
                        if len(archive) > 8 * 1024 * 1024:
                            raise ValueError("intent artifact exceeds size limit")
                    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
                        members = tar.getmembers()
                        if len(members) != 1 or not members[0].isfile():
                            raise ValueError("expected one regular artifact")
                        stream = tar.extractfile(members[0])
                        if stream is None:
                            raise ValueError("missing artifact")
                        data = stream.read(8 * 1024 * 1024 + 1)
                        if len(data) > 8 * 1024 * 1024 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
                            raise ValueError("invalid diff image")
                    name = f"intent-{check['name']}-turn{inp.turn}.png"
                    await self._publish_artifact(inp.task_id, name, data, "image/png")
                    paths.append(f"Diff image: {path} (artifact: {name})")
                except (OSError, ValueError, tarfile.TarError, SandboxError):
                    paths.append(f"Diff image unavailable: {path}")
            artifact_paths.extend(paths)
            failures.append(check["name"] + ":\n" + tail(output, 2000))
        await self.store.append_event(
            inp.task_id,
            "intents.checked",
            {"turn": inp.turn, "checks": details},
            dedup_key=f"turn{inp.turn}.intents.checked",
        )
        return VerifyResult(
            not failures,
            1 if failures else 0,
            tail("\n".join(failures), 2000) + "\n" + "\n".join(artifact_paths),
        )

    @activity.defn
    async def acquire_slot(self, task_id: str) -> dict:
        from sca.admission.pressure import gateway_pressure

        result = await self.store.acquire_slot(
            task_id, self.settings.max_running_tasks, await gateway_pressure())
        if "wait_seconds" in result:
            with metric_guard():
                business_metrics.admission_wait.observe(result["wait_seconds"])
        return result

    @activity.defn
    async def release_slot(self, task_id: str) -> None:
        await self.store.release_slot(task_id)

    # Projects
    @activity.defn
    async def project_work_blocked(self, task_id: str) -> bool:
        return await self.store.project_work_blocked(task_id)

    @activity.defn
    async def set_state(self, inp: SetStateInput) -> str | None:
        return await self._set_task_state(inp)

    # Task output directories
    async def _set_task_state(
        self, inp: SetStateInput, *, write_output: Callable[[Task], Awaitable[dict]] | None = None,
    ) -> str | None:
        try:
            previous_state = None
            if inp.state in TERMINAL_STATES:
                with metric_guard():
                    previous_state = (await self.store.get_task(inp.task_id)).state
            metadata = {"model_provider_error": inp.model_provider_error} if inp.model_provider_error else {}
            if inp.wait_for_messages:
                metadata["wait_for_messages"] = True
            if write_output is not None:
                metadata["write_output"] = write_output
            task = await self.store.set_state(inp.task_id, TaskState(inp.state), inp.reason, **metadata)
            if inp.wait_for_messages and task.state != inp.state:
                return MESSAGES_PENDING
            if task.state in TERMINAL_STATES and previous_state not in TERMINAL_STATES:
                with metric_guard():
                    business_metrics.duration.labels(task.kind, task.state).observe(
                        (datetime.now(UTC) - task.created_at).total_seconds()
                    )
                    turns = task.result.get("turns")
                    if turns is None:
                        events = await self.store.list_events(inp.task_id, limit=1000000)
                        turns = sum(e.type == EventType.AGENT_TURN_COMPLETED for e in events)
                    business_metrics.turns.labels(task.kind).observe(turns)
        except _NON_RETRYABLE as e:
            raise _non_retryable(e) from e
        return None

    @activity.defn
    async def create_sandbox(self, task_id: str) -> str:
        started = time.monotonic()
        async with self._live_task(task_id, "sandbox"):
            sandbox_id, image = await self._create_sandbox(task_id)
            payload = await self._sandbox_event_payload(sandbox_id, image)
        await self.store.append_event(
            task_id, EventType.SANDBOX_CREATED, {**payload, "duration_s": time.monotonic() - started},
            dedup_key="sandbox.created",
        )
        return sandbox_id

    def _activity_route(self) -> dict:
        if not activity.in_activity():
            return {}
        info = activity.info()
        return {"workflow_id": info.workflow_id, "task_queue": info.task_queue,
                **({"worker": self.worker_identity} if self.worker_identity else {})}

    async def _sandbox_event_payload(self, sandbox_id: str, image: str | None = None) -> dict:
        payload = {
            "sandbox_id": sandbox_id,
            "driver": self.settings.sandbox_driver,
            **self._activity_route(),
        }
        if image is not None:
            payload.update(image=image, state_transport=self.settings.state_transport,
                           cpus=self.settings.sandbox_cpus, memory_mb=self.settings.sandbox_memory_mb)
        instance_id = getattr(self.sandbox, "runtime_id", None)
        if instance_id is not None:
            with contextlib.suppress(SandboxError):
                identity = await instance_id(sandbox_id)
                if isinstance(identity, str) and identity:
                    payload["container_id"] = identity
        return payload

    @activity.defn
    async def release_sandbox(self, inp: StepInput) -> None:
        """Free the sandbox of an idle task. Workspace and session history stay on disk."""
        sandbox_id = await self._sandbox_id(inp.task_id)
        payload = await self._sandbox_event_payload(sandbox_id)
        await self._record_sandbox_activity(inp.task_id)
        await self.sandbox.destroy(sandbox_id)
        await self.store.release_slot(inp.task_id)
        await self.store.append_event(
            inp.task_id,
            EventType.SANDBOX_RELEASED,
            {**payload, "after_turn": inp.turn},
            dedup_key=f"turn{inp.turn}.sandbox_released",
        )

    @activity.defn
    async def restore_sandbox(self, inp: StepInput) -> None:
        """Bring back a released sandbox on the same workspace and session history."""
        async with self._live_task(inp.task_id, "sandbox"):
            await self._restore_sandbox(inp)

    async def _restore_sandbox(self, inp: StepInput) -> None:
        started = time.monotonic()
        spec = await self._spec(inp.task_id)
        sandbox_id, image = await self._create_sandbox(inp.task_id)
        setup = await self._setup_command(inp.task_id, spec)
        if setup:
            # Whatever setup installed outside the mounted directories went with the old sandbox.
            exit_event, output = await self._run(
                sandbox_id, ["bash", "-lc", setup], timeout_s=14 * 60
            )
            if exit_event.exit_code != 0:
                raise ApplicationError(
                    f"setup command failed on restore: {tail(output, 1000)}",
                    type="SetupFailed",
                    non_retryable=True,
                )
        payload = await self._sandbox_event_payload(sandbox_id, image)
        await self.store.append_event(
            inp.task_id,
            EventType.SANDBOX_RESTORED,
            {**payload, "after_turn": inp.turn, "duration_s": time.monotonic() - started},
            dedup_key=f"turn{inp.turn}.sandbox_restored",
        )

    async def _create_sandbox(self, task_id: str) -> tuple[str, str]:
        await self._ensure_state(task_id)
        spec = await self._spec(task_id)
        if self.settings.state_transport == "sync":
            ws = self._workspace(task_id)
            manifest = await self._state(task_id).manifest()
            if manifest is not None:
                (ws.task_dir / "workspace.ready").unlink(missing_ok=True)
                await ws.build_workspace(manifest["commit"])
        task = await self.store.get_task(task_id)
        context_keys = task.result.get("intents", {}).get("context_keys", {})
        if context_keys:
            from sca.intents.base import IntentBundle
            from sca.intents.compiler import write_context

            files = {name: await self.objects.get(key) for name, key in context_keys.items()}
            write_context(self._workspace(task_id).workspace, IntentBundle(context_files=files))
        if 'memory_context' in task.result:
            from sca.memory.context import write_memory

            write_memory(self._workspace(task_id).workspace, task.result['memory_context'])
        # The kind may wrap the backend (e.g. coding adds the OpenCode dependency mounts); the
        # kernel instance that runs this activity must ask the kind's activities, not itself.
        owner = self.scenarios.get(task.kind, self) if self.scenarios else self
        backend = owner._backend(spec)
        champion = (await self.store.get_task_champion(task_id)
                    if getattr(backend, "name", None) == "opencode" else None)
        ctx = self._agent_ctx(task_id, spec, champion)
        await self._resolve_opencode_version(ctx, backend, task_id)
        additions = backend.sandbox_additions(ctx)
        runtime_mounts = await self._docker_node_mounts(backend, spec.image)
        cache_mounts, cache_env = [], {}
        if self.settings.pip_cache and self.settings.state_transport == "mount":
            # Per tenant: a shared writable cache would let one tenant plant a package that
            # another tenant installs.
            tenant = (await self.store.get_task(task_id)).tenant_id
            cache_root = self.settings.data_dir.resolve() / "cache"
            cache_mounts, cache_env = [pip_cache_mount(cache_root, tenant)], PIP_CACHE_ENV
        sandbox_spec = SandboxSpec(
            image=spec.image or getattr(backend, "sandbox_image", None) or self.settings.sandbox_image,
            mounts=[
                *self.transport.mounts(self._task_dir(task_id)),
                *additions.mounts,
                *runtime_mounts,
                *cache_mounts,
            ],
            env={**cache_env, **additions.env},
            cpus=self.settings.sandbox_cpus,
            memory_mb=self.settings.sandbox_memory_mb,
            network=getattr(backend, "sandbox_network", self.settings.sandbox_network),
            runtime=self.settings.sandbox_runtime,
            labels={"sca.task_id": task_id, "sca.state_transport": self.settings.state_transport},
        )
        attachments = await self._tool_attachment_plan(task_id, spec)
        tool_hosts = sorted({tool.host for tool in attachments})
        if tool_hosts:
            from sca.tools.sandbox import McpSandboxSpec

            sandbox_spec = McpSandboxSpec(**sandbox_spec.model_dump(), mcp_hosts=tool_hosts)
        started = time.monotonic()
        try:
            sandbox_id = await self.sandbox.create(sandbox_name(task_id), sandbox_spec)
        finally:
            with metric_guard():
                business_metrics.sandbox_create.labels(self.settings.sandbox_driver).observe(
                    time.monotonic() - started
                )
        try:
            await unpause_sandbox(self.sandbox, sandbox_id)
            await self.transport.push(sandbox_id, self._task_dir(task_id))
            task = await self.store.get_task(task_id)
            opened = task.result.get("sandbox_opened_at") or time.time()
            await self.store.update_result(task_id, {"sandbox_id": sandbox_id,
                                                     "sandbox_opened_at": opened,
                                                     **({"tool_hosts": tool_hosts} if tool_hosts else {})})
        except BaseException:
            if tool_hosts:
                with contextlib.suppress(SandboxError):
                    await self.sandbox.destroy(sandbox_id)
            raise
        return sandbox_id, sandbox_spec.image

    async def _setup_command(self, task_id: str, spec: ExecutionSpec) -> str:
        task = await self.store.get_task(task_id)
        commands = [spec.setup_command, *task.result.get("intents", {}).get("setup", [])]
        return " && ".join(f"({c})" for c in commands if c) or "true"

    @activity.defn
    async def run_setup(self, task_id: str) -> None:
        async with self._live_task(task_id, "setup"):
            await self._run_setup(task_id)

    async def _run_setup(self, task_id: str) -> None:
        if await self._ensure_state(task_id):
            await self.store.update_result(task_id, {"workspace_forked": False})
            await self.sandbox.destroy(await self._sandbox_id(task_id))
            await self._create_sandbox(task_id)
        spec = await self._spec(task_id)
        sandbox_id = await self._sandbox_id(task_id)
        task = await self.store.get_task(task_id)
        owner = self.scenarios.get(task.kind, self)
        skip = getattr(owner, "_forked_setup", None)
        if skip is not None and await skip(task_id, sandbox_id):
            await self.store.append_event(
                task_id, EventType.SETUP_COMPLETED,
                {"skipped": True, "exit_code": 0, "snapshot_id": task.result.get("snapshot_id")},
                dedup_key="setup.completed",
            )
            return
        setup_started = time.monotonic()
        exit_event, output = await self._run(
            sandbox_id, ["bash", "-lc", await self._setup_command(task_id, spec)], timeout_s=14 * 60
        )
        await self.store.append_event(
            task_id,
            EventType.SETUP_COMPLETED,
            {
                "command": spec.setup_command,
                "exit_code": exit_event.exit_code,
                "timed_out": exit_event.timed_out,
                "output_tail": tail(output),
                "duration_s": time.monotonic() - setup_started,
                "skipped": False,
            },
            dedup_key="setup.completed",
        )
        if exit_event.exit_code != 0:
            raise ApplicationError(
                f"setup command failed with exit status {exit_event.exit_code}: {tail(output, 1000)}",
                type="SetupFailed",
                non_retryable=True,
            )

        after_setup = getattr(owner, "_snapshot_after_setup", None)
        if after_setup is not None:
            await after_setup(task_id, sandbox_id, time.monotonic() - setup_started)
        ws = self._workspace(task_id)
        if hasattr(ws, "volume"):
            from sca.workspace.layers import disk_bytes

            usage = {"workspace_bytes": await disk_bytes(ws.upper),
                     "workspace_method": "overlay", "method": "overlay"}
            await self.store.update_result(task_id, usage)
            await self.store.append_event(task_id, EventType.WORKSPACE_PREPARED,
                                          {**usage, "phase": "after_setup"},
                                          dedup_key="workspace.prepared.after_setup")

    @activity.defn
    async def run_agent_turn(self, inp: TurnInput) -> TurnResult:
        async with (self._live_task(inp.task_id, "turn"),
                    self._turn_heartbeats(interjectible=inp.interjectible)):
            return await self._run_agent_turn(inp, await self.store.get_task_champion(inp.task_id))

    @contextlib.asynccontextmanager
    async def _turn_heartbeats(self, *, interjectible: bool) -> AsyncIterator[None]:
        interval = (
            HEARTBEAT_INTERVAL.total_seconds() if interjectible
            else min(20, self.settings.turn_heartbeat_timeout_s / 4)
        )

        async def beat() -> None:
            while True:
                activity.heartbeat()
                await asyncio.sleep(interval)

        beater = asyncio.create_task(beat())
        try:
            yield
        finally:
            beater.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beater

    async def _run_agent_turn(self, inp: TurnInput, champion: dict | None = None) -> TurnResult:
        task_id, turn = inp.task_id, inp.turn
        restored = await self._ensure_state(task_id)
        attempt = activity.info().attempt
        key = f"turn{turn}.attempt{attempt}"
        if restored or attempt > 1:
            await self.sandbox.destroy(await self._sandbox_id(task_id))
            await self._state(task_id).restore(self._workspace(task_id), force=True)
            await self.restore_sandbox(StepInput(task_id, turn - 1))
        spec = await self._spec(task_id)
        agent = self._backend(spec)
        gateway = self.gateway if getattr(agent, "uses_model_gateway", True) else None
        deferred = gateway if inp.defer_spend and isinstance(gateway, DeferredSpendGateway) else None
        scope = getattr(self, "name", None)
        if deferred and attempt > 1:
            await self.settle_turn_spend(SettleTurnSpend(task_id, turn, scope))
        sandbox_id = await self._sandbox_id(task_id)
        await self._before_agent_turn(task_id, sandbox_id)
        # A session first created by the interrupted turn is absent from the snapshot.
        session_id = (
            inp.session_id
            if restored or attempt > 1
            else inp.session_id or await self.store.get_session_id(task_id)
        )

        # Local model providers
        model = read_registry(self.settings.data_dir).models.get(
            spec.model or self.settings.default_model) if gateway else None
        turn_budget = None if model and model.unpriced else self.settings.model_gateway_turn_budget_usd
        if gateway and deferred is None and turn_budget is not None and spec.max_cost_usd is not None:
            task = await self.store.get_task(task_id)
            remaining = spec.max_cost_usd - task.usage.get("cost_micro_usd", 0) / 1e6
            if remaining <= 0:
                return TurnResult(session_id=session_id, final_message="", skipped=True)
            turn_budget = min(turn_budget, remaining)

        ctx = await self._attach_tools(task_id, spec, key, champion)
        if model is not None:
            ctx.model_context_window = model.context_window
            ctx.model_registered = True
        overlay_metadata = ({"overlay_sha256": ctx.agent_overlay.sha256()}
                            if ctx.agent_overlay is not None else {})
        if overlay_metadata:
            await self.store.update_result(task_id, overlay_metadata)
        start = {"turn": turn, "attempt": attempt, "prompt": inp.prompt, "backend": agent.name,
                 "prompt_version": getattr(spec, "prompt_version", None) or EvaluationSettings().default_prompt_version,
                 **overlay_metadata, **self._activity_route()}
        if inp.guidance is None and deferred is None:
            await self.store.append_event(task_id, EventType.AGENT_TURN_STARTED, start, dedup_key=f"{key}.started")
        await self._resolve_opencode_version(ctx, agent, task_id)

        key_alias = f"task-{task_id}-turn{turn}-attempt{attempt}"
        if hasattr(self, "name"):
            key_alias += f"-{getattr(self, 'scope', self.name)}"
        if gateway:
            # The credential lives exactly as long as the turn and never enters the
            # workflow history.
            ctx.model_base_url = gateway.sandbox_url
            if getattr(spec, "tools", []):
                from sca.tools.sandbox import ToolSandboxProvider

                ctx.model_base_url = await cast(ToolSandboxProvider, self.sandbox).mcp_model_url(
                    sandbox_id, ctx.model_base_url, self.settings.model_gateway_container,
                )
        task_kind = "unknown"
        with metric_guard():
            task_kind = (await self.store.get_task(task_id)).kind
        final_message = ""
        spend = None
        cost_final = False
        turn_error: AgentTurnError | ModelProviderError | None = None
        provider_error: ModelProviderError | None = None
        n = 0
        issued = False
        reserved = False
        interrupted = False
        started = inp.guidance is None and deferred is None

        async def note_interrupted() -> None:
            if started and (inp.interjectible or inp.guidance is not None):
                await self.store.append_event(
                    task_id, "agent.turn_interrupted", {"turn": turn}, dedup_key=f"{key}.interrupted",
                )

        try:
            if gateway:
                if deferred:
                    event = {"turn": turn, "attempt": attempt, "model": ctx.model,
                             **({"subtask": scope} if scope else {})}
                    turn_budget = await self._reserve_turn_spend(task_id, key_alias, turn_budget, event)
                    if turn_budget == 0:
                        if await self.store.check_budget(task_id):
                            return TurnResult(session_id=session_id, final_message="", skipped=True)
                        raise ApplicationError(
                            "No virtual model key budget available", type="BudgetExceeded", non_retryable=True,
                        )
                    reserved = True
                # Wait for an in-progress issuance before revoking the alias on cancellation.
                issue = asyncio.create_task(gateway.issue_key(
                    key_alias,
                    models=[ctx.model or self.settings.default_model],
                    ttl_s=spec.turn_timeout_s + 120,
                    max_budget_usd=turn_budget,
                    metadata={"task_id": task_id, "turn": turn},
                ))
                try:
                    ctx.model_api_key = await asyncio.shield(issue)
                except asyncio.CancelledError:
                    with contextlib.suppress(GatewayError):
                        ctx.model_api_key = await issue
                    issued = ctx.model_api_key is not None or deferred is None
                    raise
                issued = True
                await self.store.append_event(
                    task_id, EventType.MODEL_KEY_ISSUED, {"turn": turn, "model": ctx.model},
                    dedup_key=f"{key}.model_key_issued",
                )
            if deferred and inp.guidance is None:
                await self.store.append_event(task_id, EventType.AGENT_TURN_STARTED, start, dedup_key=f"{key}.started")
                started = True
            if guide := inp.guidance:
                await self.store.start_guided_turn(
                    task_id, start, f"{key}.started", guide.message_ids, guide.interrupted_ids,
                    plan_attempt=guide.plan_attempt,
                )
                started = True
            stream = agent.run_turn(
                self.sandbox, sandbox_id, ctx, inp.prompt,
                session_id=session_id, timeout_s=spec.turn_timeout_s,
            )
            async with closing_stream(stream):
                async for ev in stream:
                    activity.heartbeat()
                    n += 1
                    if ev.type == AgentEventType.SESSION_STARTED:
                        session_id = ev.data.get("session_id") or session_id
                        if session_id:
                            await self.store.set_session(task_id, agent.name, session_id)
                        continue
                    if ev.type == AgentEventType.TURN_COMPLETED:
                        final_message = ev.data.get("final_message", "")
                        continue
                    if ev.model_call_succeeded:
                        provider_error = None
                        if self.provider_health is not None:
                            self.provider_health.record(None)
                    if ev.type == AgentEventType.ERROR:
                        caught = ModelProviderError.from_payload(ev.data.get("model_provider_error"))
                        if caught is not None:
                            if self.provider_health is not None:
                                self.provider_health.record(caught)
                            if provider_error is None or caught.is_account_error or not provider_error.is_account_error:
                                provider_error = caught
                    if ev.type == AgentEventType.USAGE and ev.data.get("summary") == "sandbox_transition":
                        await self.store.append_event(task_id, "sandbox.transition", ev.data,
                                                      dedup_key=f"{key}.{n}.suspend")
                        continue
                    if ev.type == AgentEventType.USAGE:
                        await self.store.add_usage(
                            task_id, {k: v for k, v in ev.data.items() if isinstance(v, int)}
                        )
                        with metric_guard():
                            for direction, field in (
                                ("input", "input_tokens"),
                                ("output", "output_tokens"),
                                ("cached", "cached_input_tokens"),
                            ):
                                business_metrics.tokens.labels(task_kind, direction).inc(
                                    ev.data.get(field, 0)
                                )
                    await self.store.append_event(
                        task_id,
                        _AGENT_EVENT_TYPES[ev.type],
                        {"turn": turn, **ev.data},
                        dedup_key=f"{key}.{n}",
                    )
        except asyncio.CancelledError:
            interrupted = True
            await note_interrupted()
            raise
        except (AgentTurnError, ModelProviderError) as e:
            if isinstance(e, ModelProviderError):
                provider_error = e
                if self.provider_health is not None:
                    self.provider_health.record(e)
            if provider_error is not None:
                await self.store.append_event(
                    task_id, "model.provider_error",
                    {"turn": turn, "attempt": attempt, **provider_error.payload()},
                    dedup_key=f"{key}.provider_error",
                )
            await self.store.append_event(
                task_id,
                EventType.AGENT_ERROR,
                {"turn": turn, "attempt": attempt, "message": str(e)},
                dedup_key=f"{key}.failed",
            )
            turn_error = e
        finally:
            settling = issued and not interrupted
            settlement_started = time.monotonic()
            indicator = self._live_task(task_id, "settle") if settling else contextlib.nullcontext()
            async with indicator:
                if self.settings.sandbox_suspend_idle and agent.name == "opencode-brain":
                    await self.store.append_event(task_id, "sandbox.transition",
                        {"paused": False, "at": time.time()}, dedup_key=f"{key}.suspend_end")
                if deferred and reserved:
                    if issued:
                        blocked = False
                        try:
                            blocked = await deferred.block_key(key_alias, issued_key=ctx.model_api_key)
                            if not blocked:
                                raise GatewayError("virtual model key is missing before spend settlement")
                            if not interrupted:
                                spend = await self._settle_blocked_key(deferred, task_id, key_alias, task_kind)
                                cost_final = True
                        except asyncio.CancelledError:
                            await note_interrupted()
                            if not blocked:
                                await deferred.block_key(key_alias, issued_key=ctx.model_api_key)
                            raise
                        except GatewayError:
                            if provider_error is None or not provider_error.is_account_error:
                                raise
                            activity.logger.warning("Could not finalize virtual model key after account failure")
                    else:
                        await self.store.clear_turn_spend(task_id, key_alias)
                elif gateway and issued:
                    finalize = getattr(gateway, "finalize_key", None)
                    cancelled = None
                    try:
                        settle = asyncio.create_task(finalize(key_alias) if finalize else gateway.revoke_key(key_alias))
                        try:
                            charge = await asyncio.shield(settle)
                        except asyncio.CancelledError as exc:
                            cancelled = exc
                            await note_interrupted()
                            charge = await settle
                        if finalize is not None:
                            spend, cost_final = charge
                        else:
                            spend = charge
                    except GatewayError:
                        if provider_error is None or not provider_error.is_account_error:
                            raise
                        activity.logger.warning("Could not finalize virtual model key after account failure")
                    if spend:
                        await self.store.add_usage(task_id, {"cost_micro_usd": round(spend * 1e6)})
                        with metric_guard():
                            business_metrics.cost.labels(task_kind).inc(spend)
                    if cancelled is not None:
                        raise cancelled
            if settling:
                await self._record_settlement(task_id, turn, key, time.monotonic() - settlement_started)

        if turn_error is not None:
            if provider_error is not None and provider_error.is_account_error:
                raise account_failure(provider_error) from None
            if isinstance(turn_error, ModelBudgetRefused):
                budget_error = await self._model_budget_failure(inp, key, turn_error)
                raise budget_error from None
            reason = await self.store.check_budget(task_id)
            if reason or "budget" in str(turn_error).lower():
                raise ApplicationError(
                    f"budget exceeded: {reason or 'model gateway budget refused'}",
                    type="BudgetExceeded",
                    non_retryable=True,
                ) from turn_error
            raise provider_error or turn_error

        if agent.name != "fake":
            await self.transport.pull_agent_home(sandbox_id, self._task_dir(task_id))
        else:
            await self._sync_fake_home(sandbox_id, task_id)
        await self.store.append_event(
            task_id,
            EventType.AGENT_TURN_COMPLETED,
            {"turn": turn, "final_message": final_message, "cost_final": cost_final,
             **({"cost_usd": spend or 0.0} if spend is not None or cost_final else {})},
            dedup_key=f"{key}.completed",
        )
        return TurnResult(session_id=session_id, final_message=final_message)

    # Interrupted turn spend
    async def _reserve_turn_spend(
        self, task_id: str, alias: str, cap: float | None, event: dict,
    ) -> float | None:
        budget, waiting = await self.store.reserve_turn_spend(task_id, alias, cap, event)
        if waiting:
            async with self._live_task(task_id, "settle"):
                while waiting:
                    await asyncio.sleep(0.25)
                    budget, waiting = await self.store.reserve_turn_spend(task_id, alias, cap, event)
        return budget

    async def _settle_blocked_key(
        self, gateway: DeferredSpendGateway, task_id: str, alias: str, task_kind: str,
    ) -> float:
        spend, final = await gateway.read_key_spend(alias)
        if not final or spend is None:
            raise GatewayError("virtual model key spend has not settled")
        if await self.store.apply_turn_spend(task_id, alias, spend) and spend:
            with metric_guard():
                business_metrics.cost.labels(task_kind).inc(spend)
        await gateway.delete_key(alias)
        await self.store.clear_turn_spend(task_id, alias)
        return spend

    @activity.defn
    async def settle_turn_spend(self, inp: SettleTurnSpend) -> None:
        async with self._live_task(inp.task_id, "settle", scope=(inp.scope or "", None)):
            started = time.monotonic()
            await self._settle_turn_spend(inp)
        elapsed = time.monotonic() - started
        if elapsed >= 1:
            await self._record_settlement(inp.task_id, inp.turn, f"turn{inp.turn}.deferred", elapsed, inp.scope)

    async def _settle_turn_spend(self, inp: SettleTurnSpend) -> None:
        gateway = self.gateway
        if not isinstance(gateway, DeferredSpendGateway):
            raise GatewayError("virtual model key settlement requires its model gateway")
        reservations = await self.store.turn_spend_reservations(inp.task_id, inp.turn, inp.scope)
        if reservations:
            task_kind = (await self.store.get_task(inp.task_id)).kind
            for alias, reservation in reservations.items():
                if "spend_usd" in reservation:
                    await gateway.delete_key(alias)
                elif await gateway.block_key(alias):
                    await self._settle_blocked_key(gateway, inp.task_id, alias, task_kind)
                    continue
                await self.store.clear_turn_spend(inp.task_id, alias)

    # Model gateway budget refusals
    async def _model_budget_failure(
        self, inp: TurnInput, key: str, error: ModelBudgetRefused,
    ) -> ApplicationError:
        model_responded = error.model_responded
        if not model_responded:
            # Include usage already recorded from earlier turns in this task.
            usage = (await self.store.get_task(inp.task_id)).usage
            model_responded = any(usage.get(field, 0) > 0 for field in (
                "input_tokens", "output_tokens", "cost_micro_usd",
            ))
        reason = MODEL_BUDGET_EXHAUSTED if model_responded else MODEL_BUDGET_MISCONFIGURED
        await self.store.append_event(inp.task_id, EventType.BUDGET_EXCEEDED,
                                      {"turn": inp.turn, "reason": reason}, dedup_key=f"{key}.budget")
        return ApplicationError(reason, type="BudgetExceeded", non_retryable=True)

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

    async def _record_sandbox_activity(self, task_id: str) -> None:
        task = await self.store.get_task(task_id)
        opened = task.result.get("sandbox_opened_at")
        if opened is None:
            return
        events = await self.store.list_events(task_id, limit=1000000)
        key = str(opened)
        previous = {e.payload["segment"]: e.payload for e in events
                    if e.type == "sandbox.activity" and "segment" in e.payload}
        if key not in previous:
            alive = max(0.0, time.time() - opened)
            transitions = [e.payload for e in events if e.type == "sandbox.transition"
                           and opened <= e.payload.get("at", 0) <= opened + alive]
            suspended = 0.0
            paused_at = None
            for transition in sorted(transitions, key=lambda p: p["at"]):
                if transition["paused"] and paused_at is None:
                    paused_at = transition["at"]
                elif not transition["paused"] and paused_at is not None:
                    suspended += transition["at"] - paused_at
                    paused_at = None
            if paused_at is not None:
                suspended += opened + alive - paused_at
            if not transitions:
                suspended = sum(e.payload.get("sandbox_suspended_s", 0) for e in events
                                if e.type == EventType.AGENT_USAGE
                                and e.created_at.timestamp() >= opened)
            payload = {"segment": key, "sandbox_alive_s": alive,
                       "sandbox_active_s": max(0.0, alive - suspended),
                       "sandbox_activity_ratio": max(0.0, alive - suspended) / alive if alive else 0.0}
            await self.store.append_event(task_id, "sandbox.activity", payload,
                                          dedup_key=f"sandbox.activity.{key}")
            previous[key] = payload
        alive = sum(p["sandbox_alive_s"] for p in previous.values())
        active = sum(p["sandbox_active_s"] for p in previous.values())
        ratio = active / alive if alive else 0.0
        await self.store.update_result(task_id, {"sandbox_alive_s": alive,
            "sandbox_active_s": active, "sandbox_activity_ratio": ratio,
            "sandbox_opened_at": None})
        with metric_guard():
            business_metrics.sandbox_activity.labels(task.kind).observe(ratio)

    @activity.defn
    async def destroy_sandbox(self, task_id: str) -> None:
        sandbox_id = f"sca-{sandbox_name(task_id)}"
        with contextlib.suppress(NotFoundError):
            sandbox_id = (await self.store.get_task(task_id)).result.get("sandbox_id", sandbox_id)
        with contextlib.suppress(NotFoundError):
            await self._record_sandbox_activity(task_id)
        await self.sandbox.destroy(sandbox_id)
        ws = self._workspace(task_id)
        if hasattr(ws, "remove_volume") and (ws.task_dir / "workspace.overlay").exists():
            await ws.remove_volume()
        await self.store.append_event(
            task_id,
            EventType.SANDBOX_DESTROYED,
            {"sandbox_id": sandbox_id},
            dedup_key="sandbox.destroyed",
        )

    async def _before_agent_turn(self, task_id: str, sandbox_id: str) -> None:
        """Scenario hook: work to do in the sandbox right before an agent turn."""
        task = await self.store.get_task(task_id)
        scenario = self.scenarios.get(task.kind)
        if scenario is not None and scenario is not self:
            await scenario._before_agent_turn(task_id, sandbox_id)

    async def _publish_artifact(
        self, task_id: str, name: str, data: bytes, content_type: str
    ) -> None:
        from sca.storage.local import key_for

        uri = await self.objects.put(key_for(task_id, name), data, content_type)
        await self.store.add_artifact(task_id, name, content_type, uri, len(data))

    @activity.defn
    async def render_initial_prompt(self, spec: dict) -> str:
        return render_prompt(spec, "initial")

    @activity.defn
    async def render_fix_prompt(self, spec: dict, exit_code: int, output: str) -> str:
        return render_prompt(spec, "fix", exit_code=exit_code, output=output)

    @activity.defn
    async def render_feedback_prompt(self, spec: dict, feedback: str, unresolved: list) -> str:
        return render_prompt(spec, "feedback", feedback=feedback, unresolved=unresolved)

    @activity.defn
    async def run_acceptance(self, task_id: str) -> bool | None:
        async with self._live_task(task_id, "verification"):
            return await self._run_acceptance(task_id)

    async def _run_acceptance(self, task_id: str) -> bool | None:
        import tempfile
        from uuid import UUID

        from sca.kernel.acceptance import extract_acceptance

        task = await self.store.get_task(task_id)
        if "acceptance_passed" in task.result:
            return task.result["acceptance_passed"]
        spec = await self._spec(task_id)
        file_id = getattr(spec, "acceptance_files", None)
        if not file_id:
            return None
        await self.store.for_tenant(task.tenant_id).get_file(UUID(str(file_id)))
        archive = await self.objects.get(f"tenants/{task.tenant_id}/files/{file_id}")
        await self._ensure_state(task_id)
        source = self._workspace(task_id)
        commit = (await source._git("--git-dir", str(source.mirror), "rev-parse",
                                   f"refs/heads/{source.branch}")).strip()
        # The only source is the trusted bare mirror, never the agent's working tree.
        with tempfile.TemporaryDirectory(prefix="sca-acceptance-") as directory:
            clean = GitWorkspace(Path(directory), source.branch)
            await clean.prepare(str(source.mirror), commit)
            await asyncio.to_thread(extract_acceptance, archive, clean.workspace,
                                    self.settings.max_upload_mb * 1024 * 1024)
            backend = self._backend(spec)
            runtime_mounts = await self._docker_node_mounts(backend, spec.image, node_only=True)
            sandbox_spec = SandboxSpec(
                image=spec.image or self.settings.sandbox_image,
                mounts=[*self.transport.mounts(clean.task_dir), *runtime_mounts],
                cpus=self.settings.sandbox_cpus,
                memory_mb=self.settings.sandbox_memory_mb,
                network=getattr(backend, "sandbox_network", self.settings.sandbox_network),
                runtime=self.settings.sandbox_runtime,
                labels={"sca.task_id": task_id, "sca.acceptance": "true"},
            )
            sandbox_id = await self.sandbox.create(f"acceptance-{task_id}", sandbox_spec)
            try:
                if activity.info().attempt > 1:
                    await self.sandbox.destroy(sandbox_id)
                    sandbox_id = await self.sandbox.create(f"acceptance-{task_id}", sandbox_spec)
                await self.transport.push(sandbox_id, clean.task_dir)
                code, output = await self._run(
                    sandbox_id, ["bash", "-lc", spec.acceptance_command], spec.turn_timeout_s)
            finally:
                await self.sandbox.destroy(sandbox_id)
        passed = code.exit_code == 0
        await self.store.complete_acceptance(task_id, passed, code.exit_code, tail(output))
        return passed

    # Mounted Docker runtimes for Node agents

    async def _docker_node_mounts(
        self, backend: AgentBackend, image: str | None, *, node_only: bool = False,
    ) -> list[Mount]:
        if image and (runtime := getattr(backend, "docker_runtime", None)) is not None:
            return await runtime.mounts(node_only=node_only)
        return []

    # Evolution productization and DeepSeek Harness evolution

    async def _pin_champion(self, task: Task, spec: dict) -> None:
        from sca.evolve.champions import select_champion

        use_champion = spec.get("use_champion")
        backend = spec.get("agent_backend") or self.agent.name
        if (backend not in {"opencode", "dsh"} or spec.get("agent_overlay") is not None
                or use_champion is False or "champion_id" in task.result):
            return
        # Unqualified evaluation variants measure the baseline independently of live rollout.
        if spec.get("eval_run_id") and use_champion is None:
            return
        version = spec.get("agent_version")
        if version is None:
            version = self.settings.dsh_version if backend == "dsh" else (
                await self.store.resolve_task_opencode_version(str(task.id), self.settings.opencode_default_version)
            )
        champion = await select_champion(
            self.store, task.id, task.tenant_id, version,
            spec.get("model") or self.settings.default_model, use_champion, agent_backend=backend,
        )
        selection = None
        if champion is not None:
            selection = {
                "champion_id": str(champion["id"]),
                "overlay_sha256": AgentOverlay.model_validate(champion["overlay"]).sha256(),
                "agent_version": champion["agent_version"],
                "model": champion["model"],
            }
        await self.store.pin_task_champion(str(task.id), selection)

    # MCP tool attachments

    async def _tool_attachment_plan(self, task_id: str, spec: ExecutionSpec) -> list[ToolAttachment]:
        names = getattr(spec, "tools", [])
        if not names:
            return []
        from sca.tools.runtime import plan_attachments
        from sca.tools.settings import get_tool_settings

        try:
            backend = self._backend(spec)
            if backend.name != "opencode":
                raise InvalidTaskError(f"后端 {backend.name} 不支持 tools，请选择 opencode")
            if (getattr(backend, "sandbox_network", self.settings.sandbox_network) != "restricted"
                    or not hasattr(self.sandbox, "mcp_model_url")):
                raise InvalidTaskError("tools 需要启用受限出口的 Docker 沙箱")
            task = await self.store.get_task(task_id)
            return await plan_attachments(self.store.for_tenant(task.tenant_id), get_tool_settings(),
                                          task.id, names)
        except _NON_RETRYABLE as exc:
            raise _non_retryable(exc) from exc

    async def _attach_tools(
        self, task_id: str, spec: ExecutionSpec, key: str, champion: dict | None = None,
    ) -> AgentContext:
        ctx = self._agent_ctx(task_id, spec, champion)
        attachments = await self._tool_attachment_plan(task_id, spec)
        if not attachments:
            return ctx
        from sca.tools.runtime import McpAgentContext, resolve_servers
        from sca.tools.settings import get_tool_settings

        task = await self.store.get_task(task_id)
        try:
            if sorted({tool.host for tool in attachments}) != task.result.get("tool_hosts"):
                raise InvalidTaskError("MCP 主机配置已更新，请重新提交任务以应用出口规则")
            servers = await resolve_servers(
                attachments, self.secrets, get_tool_settings(), task.id, task.tenant_id,
                spec.turn_timeout_s + 120,
            )
        except _NON_RETRYABLE as exc:
            raise _non_retryable(exc) from exc
        await self.store.append_event(task_id, "tools.attached", {"tools": [
            {"name": attachment.tool.name, "version": attachment.tool.version}
            for attachment in attachments
        ]}, dedup_key=f"{key}.tools_attached")
        return McpAgentContext(**ctx.model_dump(), mcp_servers=servers)

    # Task interjection

    @activity.defn
    async def deliver_task_messages(self, inp: DeliverMessages) -> list[Guidance]:
        if inp.message_ids is not None:
            from uuid import UUID

            # Another subtask may already have applied a message addressed to both agents.
            ids = set(inp.message_ids)
            rows = [row for row in await self.store.list_task_messages(UUID(inp.task_id)) if str(row.id) in ids]
        elif inp.pending_only:
            rows = await self.store.pending_task_messages(inp.task_id)
        else:
            rows = await self.store.deliver_task_messages(inp.task_id, inp.turn, planning=inp.planning)
        return [Guidance(str(row.id), row.text) for row in rows]

    @activity.defn
    async def apply_task_messages(self, inp: ApplyMessages) -> None:
        await self.store.apply_task_messages(inp.task_id, inp.message_ids)

    # Live conversation
    def _live_scope(self) -> LaneScope:
        return ("", None)

    def _live_task(
        self, task_id: str, operation: str, *, scope: LaneScope | None = None,
    ) -> contextlib.AbstractAsyncContextManager[None]:
        return worker_activity(self.settings.data_dir, "task", task_id, operation, scope or self._live_scope())

    async def _record_settlement(
        self, task_id: str, turn: int, key: str, duration_s: float, scope: str | None = None,
    ) -> None:
        with contextlib.suppress(Exception):
            await self.store.append_event(
                task_id, EventType.AGENT_ACTIVITY,
                {"turn": turn, "operation": "settle", "status": "completed", "duration_s": duration_s,
                 **({"subtask": scope} if scope else {})},
                dedup_key=f"{key}.{scope or 'main'}.settle.completed",
            )
