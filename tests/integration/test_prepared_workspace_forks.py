"""Prepared workspace reuse with real Postgres/Git and a fake sandbox transport."""
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.testing import ActivityEnvironment

from sca.config import Settings
from sca.core.models import ExecExit
from sca.kernel.activities import KernelActivities
from sca.kinds import builtin
from sca.kinds.coding.activities import CodingActivities
from sca.storage.local import LocalObjectStore
from sca.workspace.layers import command
from sca.workspace.tarsafe import chunks, pack

builtin.load()  # registers the coding kind the activities look up

pytestmark = pytest.mark.integration


async def test_prepared_task_forks_and_skips_setup(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("value = 1\n")
    await command("git", "-C", str(repo), "init", "-b", "main")
    await command("git", "-C", str(repo), "add", ".")
    await command("git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@invalid",
                  "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
    settings = Settings(data_dir=tmp_path / "data", workspace_snapshots=True,
                        workspace_overlay=False, workspace_layers=False, repo_cache=False,
                        sandbox_driver="docker", state_transport="mount")
    objects = LocalObjectStore(tmp_path / "objects")
    sandbox = SimpleNamespace()
    coding = CodingActivities(settings=settings, store=store, sandbox=sandbox,
                              agent=SimpleNamespace(name="fake"), objects=objects)
    spec = {"repo_url": str(repo), "instruction": "Fix", "setup_command": "install",
            "labels": {"fork_from_snapshot": "true"}}
    first, _ = await store.create_task(tenant_id="default", kind="coding", spec=spec, idempotency_key=None)
    first_id = str(first.id)
    env = ActivityEnvironment()
    await env.run(coding.prepare_workspace, first_id)
    prepared = coding._workspace(first_id).workspace
    (prepared / "installed").write_text("dependency")

    def download(*_):
        stream = io.BytesIO()
        pack(prepared, stream)
        stream.seek(0)
        return chunks(stream)

    sandbox.download = download
    await coding._snapshot_after_setup(first_id, "fake", 3.0)
    second, _ = await store.create_task(tenant_id="default", kind="coding", spec=spec, idempotency_key=None)
    second_id = str(second.id)
    await env.run(coding.prepare_workspace, second_id)
    fork = coding._workspace(second_id).workspace
    assert (fork / "installed").read_text() == "dependency"
    (fork / "installed").write_text("private")
    assert (prepared / "installed").read_text() == "dependency"
    task = await store.get_task(second_id)
    assert task.result["workspace_forked"] is True
    events = await store.list_events(second_id)
    event = next(e for e in events if e.type == "workspace.forked")
    assert event.payload["saved_seconds"] >= 3
    kernel = KernelActivities(settings=settings, store=store, sandbox=sandbox,
                              agent=SimpleNamespace(name="fake"), objects=objects)
    kernel.scenarios = {"coding": coding}
    kernel._ensure_state = AsyncMock(return_value=False)
    kernel._sandbox_id = AsyncMock(return_value="fake")
    coding._run = AsyncMock(return_value=(ExecExit(exit_code=0), ""))
    kernel._run = AsyncMock(side_effect=AssertionError("setup must not run"))
    await env.run(kernel.run_setup, second_id)
    await env.run(kernel.run_setup, second_id)
    setup = [e for e in await store.list_events(second_id) if e.type == "setup.completed"]
    assert len(setup) == 1
    assert setup[0].payload["skipped"] is True
    assert coding._run.call_args.args[1][-2:] == [coding._workspace(second_id).branch,
                                               task.result["base_sha"]]
    (fork.parent / "workspace.snapshot").unlink()
    assert not await coding._forked_setup(second_id, "fake")
    assert not (await store.get_task(second_id)).result["workspace_forked"]
    other, _ = await store.create_task(tenant_id="other", kind="coding", spec=spec, idempotency_key=None)
    await env.run(coding.prepare_workspace, str(other.id))
    assert not (coding._workspace(str(other.id)).workspace / "installed").exists()
    await coding._snapshots().prune(0)
