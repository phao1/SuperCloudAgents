"""Full stack in one process: real Postgres, Temporal and Docker; API and worker in-process."""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from temporalio.client import Client

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.db.engine import make_engine, make_sessionmaker
from sca.db.repo import Store
from sca.orchestration.client import TemporalOrchestrator
from sca.orchestration.worker import build_worker
from sca.storage import make_object_store

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """A bare repo with a buggy function and a failing test, as the task's target."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (seed / "test_calc.py").write_text(
        "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n"
    )
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "initial")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    return bare


@dataclass
class Stack:
    http: httpx.AsyncClient
    settings: Settings
    store: Store
    submitted: list[str] = field(default_factory=list)

    async def submit(self, spec: dict) -> str:
        r = await self.http.post("/v1/tasks", json={"kind": "coding", "spec": spec})
        assert r.status_code == 201, r.text
        self.submitted.append(r.json()["id"])
        return r.json()["id"]

    async def task(self, task_id: str) -> dict:
        r = await self.http.get(f"/v1/tasks/{task_id}")
        r.raise_for_status()
        return r.json()

    async def events(self, task_id: str) -> list[dict]:
        events: list[dict] = []
        while True:
            after = events[-1]["seq"] if events else 0
            r = await self.http.get(
                f"/v1/tasks/{task_id}/events", params={"after": after, "limit": 500}
            )
            r.raise_for_status()
            page = r.json()
            events += page
            if len(page) < 500:
                return events

    async def wait_for(self, task_id: str, state: str, timeout: float = 120, since_seq: int = 0):
        """Wait until the task enters `state` after event `since_seq`."""
        async with asyncio.timeout(timeout):
            while True:
                task = await self.task(task_id)
                entered = [
                    e
                    for e in await self.events(task_id)
                    if e["type"] == "task.state_changed"
                    and e["seq"] > since_seq
                    and e["payload"]["to"] == state
                ]
                if entered and task["state"] == state:
                    return task
                if task["state"] in TERMINAL and task["state"] != state:
                    tail = [(e["type"], e["payload"]) for e in (await self.events(task_id))[-6:]]
                    raise AssertionError(
                        f"task ended {task['state']}: {task['state_reason']}\n{tail}"
                    )
                await asyncio.sleep(0.5)

    async def review(self, task_id: str, decision: str, feedback: str | None = None) -> None:
        r = await self.http.post(
            f"/v1/tasks/{task_id}/review", json={"decision": decision, "feedback": feedback}
        )
        assert r.status_code == 200, r.text

    async def patch(self, task_id: str) -> str:
        r = await self.http.get(f"/v1/tasks/{task_id}/artifacts/changes.patch")
        r.raise_for_status()
        return r.text


@pytest.fixture
def state_transport() -> str:
    return os.environ.get("SCA_E2E_STATE_TRANSPORT", os.environ.get("SCA_STATE_TRANSPORT", "mount"))


@pytest.fixture
def agent_backend() -> str:
    return "fake"


@pytest.fixture
def settings_overrides() -> dict:
    return {}


@pytest.fixture
def temporal_interceptors() -> list:
    return []


@pytest.fixture
def in_process_worker() -> bool:
    return True


@pytest.fixture
async def stack(
    tmp_path: Path,
    agent_backend: str,
    state_transport: str,
    settings_overrides: dict,
    in_process_worker: bool,
    temporal_interceptors: list,
):
    settings = Settings(
        data_dir=tmp_path / "data",
        agent_backend=agent_backend,
        task_queue=f"e2e-{uuid.uuid4()}",
        **{"state_transport": state_transport, "maintenance_schedule": False, **settings_overrides},
    )
    engine = make_engine(settings.database_url)
    store = Store(make_sessionmaker(engine))
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        interceptors=temporal_interceptors,
    )
    worker = worker_task = None
    if in_process_worker:
        worker = await build_worker(client, settings, store)
        worker_task = asyncio.create_task(worker.run())
    app = create_app(
        store=store,
        orchestrator=TemporalOrchestrator(client, settings.task_queue),
        authenticator=StaticTokenAuthenticator({"e2e-token": "default"}),
        object_store=make_object_store(settings),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://sca.test",
        headers={"Authorization": "Bearer e2e-token"},
        timeout=30,
    ) as http:
        try:
            stack = Stack(http=http, settings=settings, store=store)
            yield stack
        finally:
            # A test that ends while its task still waits (e.g. for review) leaves the sandbox
            # running: nothing would ever finish that task. Remove what this test created.
            for task_id in stack.submitted if "stack" in locals() else []:
                remove_sandboxes(task_id)
            if "stack" in locals() and stack.submitted:
                # Unfinished tasks of this test would count against the tenant's active-task
                # quota in the shared database forever.
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE tasks SET state = 'CANCELLED', "
                            "state_reason = 'test ended before the task finished' "
                            "WHERE id = ANY(:ids) "
                            "AND state NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED')"
                        ),
                        {"ids": [uuid.UUID(t) for t in stack.submitted]},
                    )
            if worker:
                await worker.shutdown()
                await worker_task
            await engine.dispose()


def remove_sandboxes(task_id: str) -> None:
    ids = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=sca.task_id={task_id}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, check=False)


def sandbox_running(task_id: str) -> bool:
    out = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"label=sca.task_id={task_id}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return bool(out.strip())
