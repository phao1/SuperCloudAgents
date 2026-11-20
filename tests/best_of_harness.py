"""Small local Git fixture and fake I/O for the Best-of-N workflow tests."""

import asyncio
import io
import os
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from sca.api.routes import router
from sca.config import Settings
from sca.core.errors import AuthError, ConflictError, InvalidTaskError, NotFoundError
from sca.core.models import (
    AgentContext,
    AgentEvent,
    ExecExit,
    Mount,
    Principal,
    SandboxAdditions,
    SandboxSpec,
    Task,
    TaskState,
    can_transition,
)
from sca.kernel.activities import KernelActivities
from sca.kernel.kind import KindDeps
from sca.kinds.coding.activities import CodingActivities
from sca.kinds.coding.kind import CodingKind
from sca.kinds.coding.spec import CodingSpec
from sca.storage.local import LocalObjectStore
from sca.workspace.git import GitWorkspace
from sca.workspace.tarsafe import chunks, extract, pack, receive

TASK_ID = UUID("1ac8c532-2a18-43b8-a36b-8aa710cc733e")
TASK_TIME = datetime(2026, 1, 1, tzinfo=UTC)


class FakeStore:
    def __init__(self, spec: dict) -> None:
        self.task = Task(id=TASK_ID, tenant_id="team", kind="coding", spec=spec,
                         state=TaskState.QUEUED, created_at=TASK_TIME, updated_at=TASK_TIME)
        self.events = [("task.created", {"spec": spec, "actor": "member"})]
        self.dedup_keys = set()
        self.artifacts = {}
        self.session = None
        self.states = {state: asyncio.Event() for state in TaskState}

    def for_tenant(self, tenant_id: str):
        return self if tenant_id == self.task.tenant_id else MissingTenant()

    async def get_task_champion(self, task_id) -> None:
        return None

    async def pin_task_champion(self, task_id, selection) -> None:
        return None

    async def get_task(self, task_id, tenant_id=None) -> Task:
        if str(task_id) != str(self.task.id) or tenant_id not in (None, self.task.tenant_id):
            raise NotFoundError("Task not found")
        return self.task.model_copy(deep=True)

    async def set_state(self, task_id, state, reason=None) -> Task:
        previous = self.task.state
        if previous != state:
            assert can_transition(previous, state), (previous, state)
            self.task.state = state
            self.task.state_reason = reason
            await self.append_event(task_id, "task.state_changed",
                                    {"from": previous, "to": state, "reason": reason})
        self.states[state].set()
        return await self.get_task(task_id)

    async def append_event(self, task_id, type_, payload, dedup_key=None) -> None:
        if dedup_key is not None:
            if dedup_key in self.dedup_keys:
                return
            self.dedup_keys.add(dedup_key)
        self.events.append((str(type_), payload))

    async def list_events(self, task_id, limit):
        from sca.core.models import Event

        return [Event(task_id=task_id, type=type_, payload=payload, seq=i, created_at=TASK_TIME)
                for i, (type_, payload) in enumerate(self.events[:limit], 1)]

    async def update_result(self, task_id, patch) -> None:
        self.task.result.update(patch)

    async def add_usage(self, task_id, usage) -> None:
        for key, amount in usage.items():
            self.task.usage[key] = self.task.usage.get(key, 0) + amount

    async def check_budget(self, task_id) -> str | None:
        limit = self.task.spec.get("max_cost_usd")
        if limit is not None and self.task.usage.get("cost_micro_usd", 0) >= limit * 1_000_000:
            return "Task cost budget exhausted"
        return None

    async def acquire_slot(self, *args) -> dict:
        return {"granted": True}

    async def release_slot(self, task_id) -> None:
        pass

    async def get_session_id(self, task_id) -> str | None:
        return self.session

    async def set_session(self, task_id, backend, session) -> None:
        self.session = session

    async def add_artifact(self, task_id, name, content_type, uri, size) -> None:
        from sca.core.models import Artifact

        self.artifacts[name] = Artifact(task_id=task_id, name=name, content_type=content_type,
                                        uri=uri, size_bytes=size, created_at=TASK_TIME)

    async def get_artifact(self, task_id, name):
        return self.artifacts[name]

    async def accept_memories(self, task_id, ids, actor, vectors) -> None:
        assert not ids


class MissingTenant:
    async def get_task(self, *args):
        raise NotFoundError("Task not found")


@dataclass
class FakeSandboxInstance:
    root: Path
    workspace: Path
    agent_home: Path


class FakeSandbox:
    def __init__(self, root: Path, driver: str) -> None:
        self.root = root
        self.driver = driver
        self.instances: dict[str, FakeSandboxInstance] = {}
        self.setup_calls: list[str] = []
        self.checks: list[tuple[str, str]] = []

    async def create(self, name: str, spec: SandboxSpec) -> str:
        sandbox_id = f"sca-{name}"
        if sandbox_id not in self.instances:
            mounts = {mount.target: Path(mount.source) for mount in spec.mounts}
            root = self.root / sandbox_id
            root.mkdir(parents=True, exist_ok=True)
            self.instances[sandbox_id] = FakeSandboxInstance(
                root, mounts.get("/workspace", root / "workspace"),
                mounts.get("/agent_home", root / "agent_home"),
            )
        return sandbox_id

    async def destroy(self, sandbox_id: str) -> None:
        self.instances.pop(sandbox_id, None)

    async def exec(self, sandbox_id, argv, *, workdir=None, timeout_s=None):
        instance = self.instances[sandbox_id]
        if argv == ["bash", "-lc", "(prepare-fixture)"]:
            self.setup_calls.append(sandbox_id)
            (instance.workspace / "node_modules").mkdir(exist_ok=True)
            (instance.workspace / "node_modules/fixture").write_text("installed")
            yield ExecExit(exit_code=0)
            return
        if argv[:2] == ["bash", "-lc"] and argv[2].startswith("check-"):
            command = argv[2]
            self.checks.append((sandbox_id, command))
            passed = {
                "check-result": (instance.workspace / "app.txt").read_text().startswith("repaired\n"),
                "check-dependency": (instance.workspace / "node_modules/fixture").is_file(),
            }[command]
            yield ExecExit(exit_code=0 if passed else 1)
            return
        assert argv[0] in {"git", "env"}
        command = [str(instance.root / "sca.bundle") if arg == "/tmp/sca.bundle" else arg for arg in argv]
        date = TASK_TIME.isoformat()
        proc = await asyncio.create_subprocess_exec(*command, cwd=instance.workspace,
                                                    env={**os.environ, "GIT_AUTHOR_DATE": date,
                                                         "GIT_COMMITTER_DATE": date},
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
        _, error = await proc.communicate()
        assert proc.returncode == 0, error.decode()
        yield ExecExit(exit_code=proc.returncode)

    async def download(self, sandbox_id, path):
        instance = self.instances[sandbox_id]
        stream = io.BytesIO()
        if path == "/workspace/." and self.driver == "docker":
            pack(instance.workspace, stream)
        else:
            path = {"/workspace/.": instance.workspace, "/agent_home": instance.agent_home,
                    "/tmp/sca.bundle": instance.root / "sca.bundle"}[path]
            with tarfile.open(fileobj=stream, mode="w") as tar:
                tar.add(path, arcname=path.name)
        stream.seek(0)
        async for chunk in chunks(stream):
            yield chunk

    async def upload(self, sandbox_id, path, archive):
        instance = self.instances[sandbox_id]
        stream = io.BytesIO()
        await receive(archive, stream)
        extract(stream, {"/workspace": instance.workspace, "/agent_home": instance.agent_home}[path])


class FakeAgent:
    name = "fake"
    state_transport = "mount"

    def __init__(self, concurrency: int) -> None:
        self.concurrency = concurrency
        self.started: list[tuple[AgentContext, str, str | None]] = []
        self.parallel = asyncio.Event()

    def sandbox_additions(self, ctx: AgentContext) -> SandboxAdditions:
        if self.state_transport == "mount":
            Path(ctx.agent_home_host).mkdir(parents=True, exist_ok=True)
            return SandboxAdditions(mounts=[Mount(source=ctx.agent_home_host, target=ctx.agent_home)])
        return SandboxAdditions()

    async def run_turn(self, sandbox, sandbox_id, ctx, prompt, *, session_id, timeout_s):
        self.started.append((ctx, sandbox_id, session_id))
        if len(self.started) == self.concurrency:
            self.parallel.set()
        async with asyncio.timeout(8):
            await self.parallel.wait()
        workspace = sandbox.instances[sandbox_id].workspace
        assert (workspace / "node_modules/fixture").read_text() == "installed"
        answer = {"wide": "repaired\nmore\nlines\n", "partial": "unfinished\n"}.get(ctx.model, "repaired\n")
        (workspace / "app.txt").write_text(answer)
        if session_id is None:
            yield AgentEvent(type="session_started", data={"session_id": f"fake-{ctx.task_id}"})
        yield AgentEvent(type="usage", data={"input_tokens": 1, "output_tokens": 1})
        yield AgentEvent(type="turn_completed", data={"final_message": "done"})


class FakeGateway:
    sandbox_url = "http://local-gateway.invalid"

    def __init__(self) -> None:
        self.keys = {}

    async def issue_key(self, alias, *, models, max_budget_usd, ttl_s, metadata):
        self.keys[alias] = (models[0], max_budget_usd)
        return "fake-model-key"

    async def finalize_key(self, alias) -> tuple[float, bool]:
        model, cap = self.keys[alias]
        cost = {"wide": .2, "cheap": .1, "partial": .05}.get(model, 0)
        return min(cost, cap), True


@dataclass
class Harness:
    store: FakeStore
    sandbox: FakeSandbox
    agent: FakeAgent
    gateway: FakeGateway
    objects: LocalObjectStore
    kernel: KernelActivities
    coding: CodingActivities
    activities: list


async def make_repo(path: Path) -> GitWorkspace:
    repo = GitWorkspace(path, "main")
    path.mkdir(parents=True)
    await repo._git("init", "-b", "main")
    (path / "app.txt").write_text("broken\n")
    await repo._git("add", "app.txt")
    await repo._git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
    return repo


def make_harness(path: Path, repo: GitWorkspace, *, concurrency=1, driver="docker", transport="mount",
                 use_gateway=True, **options) -> Harness:
    spec = CodingSpec(repo_url=str(repo.task_dir), instruction="Fix app.txt", memory_enabled=False,
                      setup_command="prepare-fixture", verify_commands=["check-result", "check-dependency"],
                      **options)
    store = FakeStore(spec.model_dump())
    sandbox, agent, gateway = FakeSandbox(path / "sandboxes", driver), FakeAgent(concurrency), FakeGateway()
    objects = LocalObjectStore(path / "objects")
    settings = Settings(_env_file=None, data_dir=path / "data", repo_cache=False, pip_cache=False,
                        state_transport=transport, sandbox_driver=driver, default_model="base-model")
    deps = KindDeps(settings, store, sandbox, agent, objects, gateway if use_gateway else None)
    activities = CodingKind().activities(deps)
    coding = activities[0].__self__
    kernel = KernelActivities(**{f: getattr(deps, f) for f in deps.__dataclass_fields__})
    kernel.scenarios = {"coding": coding}
    return Harness(store, sandbox, agent, gateway, objects, kernel, coding, kernel.kernel_all() + activities)


def api_client(harness: Harness, orchestrator: Any) -> httpx.AsyncClient:
    class Auth:
        async def authenticate(self, token):
            if token == "member":
                return Principal(tenant_id="team", subject="reviewer", role="member")
            if token == "viewer":
                return Principal(tenant_id="team", subject="viewer", role="viewer")
            if token == "other":
                return Principal(tenant_id="another-team", subject="other")
            raise AuthError("Unauthenticated")

    class Counter:
        def inc(self):
            pass

    app = FastAPI()
    app.include_router(router)
    app.state.store = harness.store
    app.state.orchestrator = orchestrator
    app.state.authenticator = Auth()
    app.state.object_store = harness.objects
    app.state.metrics = type("Metrics", (), {"reviews": Counter()})()

    from sca.api.authz import ForbiddenError

    async def map_error(_: Request, exc: Exception) -> JSONResponse:
        status = {AuthError: 401, ForbiddenError: 403, NotFoundError: 404,
                  ConflictError: 409, InvalidTaskError: 422}[type(exc)]
        return JSONResponse({"detail": str(exc)}, status_code=status)

    for error in (AuthError, ForbiddenError, NotFoundError, ConflictError, InvalidTaskError):
        app.add_exception_handler(error, map_error)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                             headers={"Authorization": "Bearer member"})
