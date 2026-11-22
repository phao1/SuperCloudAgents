"""The task activity pins the rollout decision and uses it when creating a sandbox."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from sca.agents.dsh import DshBackend
from sca.agents.opencode import OpenCodeBackend
from sca.config import Settings
from sca.core.models import AgentOverlay, Task
from sca.evolve.champions import rollout_bucket, select_champion
from sca.kernel.activities import KernelActivities, TurnInput
from sca.kinds import builtin
from sca.kinds.coding.spec import CodingSpec


@pytest.mark.parametrize("use,override,default,expected", [
    (None, None, 100, "rollout"),
    (None, None, None, None),
    (None, 0, 100, None),
    (None, 100, 0, "rollout"),
    (True, 0, 0, "active"),
    (False, 100, 100, None),
])
async def test_tenant_and_platform_rollout(use, override, default, expected):
    task_id = UUID("df25d2ea-9beb-44ce-a96b-dc658264a37e")

    def row(percent):
        return {"champion_id": "rollout", "percent": percent} if percent is not None else None

    platform = SimpleNamespace(
        get_active_champion=AsyncMock(return_value={"id": "active"}),
        get_champion=AsyncMock(return_value={"id": "rollout"}),
        get_champion_rollout=AsyncMock(return_value=row(default)),
    )
    tenant = SimpleNamespace(get_champion_rollout=AsyncMock(return_value=row(override)))
    store = SimpleNamespace(for_tenant=lambda name: platform if name == "eval" else tenant)
    chosen = await select_champion(store, task_id, "team", "v", "m", use)
    assert (chosen["id"] if chosen else None) == expected
    if override is not None or use is not None:
        platform.get_champion_rollout.assert_not_awaited()


async def test_rollout_cutoff_uses_stable_task_id_bucket():
    task_id = UUID("df25d2ea-9beb-44ce-a96b-dc658264a37e")
    bucket = rollout_bucket(task_id)
    assert bucket == rollout_bucket(str(task_id))
    assert 0 <= bucket < 100
    row = {"percent": bucket, "champion_id": uuid4()}
    expected = {"id": row["champion_id"]}
    tenant = SimpleNamespace(
        get_champion_rollout=AsyncMock(return_value=row), get_champion=AsyncMock(return_value=expected),
    )
    root = SimpleNamespace(for_tenant=lambda _: tenant)
    assert await select_champion(root, task_id, "eval", "v", "m", None) is None
    row["percent"] += 1
    assert await select_champion(root, task_id, "eval", "v", "m", None) == expected


def task_for(spec):
    return Task(id=uuid4(), tenant_id="team", kind="coding", state="QUEUED", spec=spec.model_dump(),
                created_at=datetime.now(UTC), updated_at=datetime.now(UTC))


async def test_load_task_pins_selection_used_by_sandbox_and_turn(tmp_path):
    builtin.load()
    version = "2.3.0-sca.5"
    directory = tmp_path / "versions" / version
    directory.mkdir(parents=True)
    (directory / "opencode").touch()
    settings = Settings(
        _env_file=None, data_dir=tmp_path, opencode_default_version=version,
        opencode_versions_dir=directory.parent, default_model="m", pip_cache=False,
    )
    agent = OpenCodeBackend(directory.parent, version, "m")
    task = task_for(CodingSpec(repo_url="repo", instruction="change", use_champion=True))
    task_id = str(task.id)
    champion = {"id": uuid4(), "agent_version": version, "model": "m",
                "overlay": {"tool_guidance": "Pinned guidance"}}
    platform = SimpleNamespace(get_active_champion=AsyncMock(return_value=champion))

    async def update(_, values):
        task.result.update(values)

    async def get_pinned(_):
        assert task.result["champion_id"] == str(champion["id"])
        return champion

    store = SimpleNamespace(
        get_task=AsyncMock(return_value=task), update_result=update,
        for_tenant=lambda name: platform, pin_task_champion=AsyncMock(side_effect=update),
        get_task_champion=get_pinned,
        resolve_task_opencode_version=AsyncMock(side_effect=lambda task_id, default: default),
    )
    sandbox = SimpleNamespace(create=AsyncMock(return_value="sandbox"))
    acts = KernelActivities(settings=settings, store=store, sandbox=sandbox, agent=agent,
                            objects=SimpleNamespace())
    acts.transport = SimpleNamespace(mounts=lambda _: [], push=AsyncMock())
    acts._ensure_state = AsyncMock(return_value=False)
    await acts.load_task(task_id)
    platform.get_active_champion.return_value = {**champion, "id": uuid4(), "overlay": {}}
    await acts.load_task(task_id)
    platform.get_active_champion.assert_awaited_once_with(version, "m", agent_backend="opencode")
    store.pin_task_champion.assert_awaited_once()
    assert task.result["overlay_sha256"] == AgentOverlay.model_validate(champion["overlay"]).sha256()
    assert "overlay" not in task.result

    await acts._create_sandbox(task_id)
    assert sandbox.create.call_args.args[1].env["SCA_AGENT_OVERLAY"] == "/agent_home/sca-overlay.json"
    overlay = settings.task_dir(task_id) / "agent_home" / "sca-overlay.json"
    assert AgentOverlay.model_validate_json(overlay.read_bytes()).tool_guidance == "Pinned guidance"
    acts._run_agent_turn = AsyncMock()
    turn = TurnInput(task_id, 1, "prompt")
    await acts.run_agent_turn(turn)
    acts._run_agent_turn.assert_awaited_once_with(turn, champion)


@pytest.mark.parametrize("use_champion", [True, None])
async def test_dsh_uses_its_own_active_champion_or_rollout(tmp_path, use_champion):
    builtin.load()
    settings = Settings(_env_file=None, data_dir=tmp_path, default_model="m")
    champion = {"id": uuid4(), "agent_backend": "dsh", "agent_version": settings.dsh_version, "model": "m",
                "overlay": {"system_prompt": "Pinned DeepSeek Harness guidance"}}
    platform = SimpleNamespace(get_active_champion=AsyncMock(return_value=champion),
                               get_champion=AsyncMock(return_value=champion))
    tenant = SimpleNamespace(get_champion_rollout=AsyncMock(
        return_value={"percent": 100, "champion_id": champion["id"]},
    ))
    store = SimpleNamespace(for_tenant=lambda name: platform if name == "eval" else tenant,
                            pin_task_champion=AsyncMock(), resolve_task_opencode_version=AsyncMock())
    acts = KernelActivities(settings=settings, store=store, sandbox=SimpleNamespace(),
                            agent=DshBackend("m"), objects=SimpleNamespace())
    spec = CodingSpec(repo_url="repo", instruction="change", agent_backend="dsh", use_champion=use_champion)
    task = task_for(spec)
    await acts._pin_champion(task, task.spec)
    lookup = platform.get_active_champion if use_champion else tenant.get_champion_rollout
    lookup.assert_awaited_once_with(settings.dsh_version, "m", agent_backend="dsh")
    store.resolve_task_opencode_version.assert_not_awaited()
    assert store.pin_task_champion.call_args.args[1]["champion_id"] == str(champion["id"])
    ctx = acts._agent_ctx(str(task.id), spec, champion)
    assert ctx.agent_version == settings.dsh_version
    assert ctx.agent_overlay.model_dump(exclude_none=True) == champion["overlay"]
    assert acts._agent_ctx(str(task.id), spec, {**champion, "agent_backend": "opencode"}).agent_overlay is None


@pytest.mark.parametrize("overrides", [
    {"agent_overlay": {}, "use_champion": True},
    {"use_champion": False},
    {"agent_backend": "codex", "use_champion": True},
    {"eval_run_id": "eval"},
])
async def test_only_eligible_tasks_resolve_champions(tmp_path, overrides):
    acts = object.__new__(KernelActivities)
    acts.settings = Settings(_env_file=None, data_dir=tmp_path)
    acts.agent = SimpleNamespace(name="opencode")
    acts.store = SimpleNamespace(pin_task_champion=AsyncMock())
    task = task_for(CodingSpec(repo_url="repo", instruction="change", **overrides))
    await acts._pin_champion(task, task.spec)
    acts.store.pin_task_champion.assert_not_awaited()
