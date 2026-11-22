"""DeepSeek Harness evolution through the API, activities and tenant-scoped registry."""

import asyncio
import json
import socket
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from sca.agents.dsh import DshBackend
from sca.api import app, evolutions
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings, get_settings
from sca.core.errors import ConflictError, NotFoundError
from sca.core.models import ExecExit, ExecOutput
from sca.db.schema import champion_rollouts, champions, evolution_schedules
from sca.eval.activities import EvalActivities
from sca.evolve.activities import EvolutionActivities
from sca.evolve.champions import select_champion
from sca.kernel.activities import KernelActivities
from sca.kinds.coding.spec import CodingSpec
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def postgres_available():
    url = make_url(get_settings().database_url)
    try:
        with socket.create_connection((url.host or "localhost", url.port or 5432), timeout=2):
            pass
    except OSError as exc:
        pytest.skip(f"Postgres unavailable: {type(exc).__name__}")


@pytest.fixture
def suite(tmp_path):
    root = tmp_path / "suites" / "coding"
    for split in ("dev", "holdout"):
        case = root / "cases" / split
        (case / "repo").mkdir(parents=True)
        (case / "repo/app.py").write_text("value = 1\n", encoding="utf-8")
        (case / "acceptance").mkdir()
        (case / "acceptance/hidden.py").write_text("assert True\n", encoding="utf-8")
        (case / "acceptance.yaml").write_text("command: python hidden.py\n", encoding="utf-8")
        (case / "task.yaml").write_text(f"split: {split}\ninstruction: 补测试\n", encoding="utf-8")
    (root / "suite.yaml").write_text("name: coding\nkind: coding\n", encoding="utf-8")
    return root


async def test_dsh_api_runs_dev_variant_with_runtime_overlay(api_store, store, suite, tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", eval_suites_dir=suite.parent,
                        agent_backend="fake", sandbox_driver="docker")
    for module in (app, evolutions):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.delenv("SCA_PORTAL_LOCAL_CONFIG", raising=False)
    temporal = SimpleNamespace(client=SimpleNamespace(start_workflow=AsyncMock()), start_task=AsyncMock())
    objects = LocalObjectStore(tmp_path / "objects")
    api = app.create_app(
        store=api_store, object_store=objects, orchestrator=temporal,
        authenticator=StaticTokenAuthenticator({"fixture-eval": "eval:admin"}),
    )

    def http():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://test",
            headers={"Authorization": "Bearer fixture-eval"},
        )

    async with http() as client:
        for backend, version in (("pi", "v"), ("dsh", "0.1.0")):
            rejected = await client.post("/v1/evolutions", json={
                "suite": suite.name, "agent_backend": backend, "agent_version": version,
            })
            assert rejected.status_code == 422
        temporal.client.start_workflow.assert_not_awaited()
        response = await client.post("/v1/evolutions", json={
            "suite": suite.name, "agent_backend": "dsh", "repeat": 1,
        })
        assert response.status_code == 201
        detail = await client.get(f"/v1/evolutions/{response.json()['id']}")
        assert detail.json()["config"]["agent_backend"] == "dsh"
    request = temporal.client.start_workflow.call_args.args[1]
    guidance = {"system_prompt": "Check the result in {{literal}}."}
    worker = EvolutionActivities(settings, store, None)
    payload = await worker.prepare_run({
        **request, "run_id": str(uuid4()), "split": "dev", "overlay": guidance, "remaining": .1,
    })
    run = await store.get_eval_run(payload["id"])
    assert run["variant"]["variant"] == {
        "agent_backend": "dsh", "agent_version": settings.dsh_version, "model": "deepseek-flash",
        "agent_overlay": guidance, "use_champion": False,
    }
    evaluation = EvalActivities(settings, store)
    monkeypatch.setattr(evaluation, "http", http)
    case, = (await evaluation.prepare(payload))["cases"]
    assert case["split"] == "dev"
    task_id = await evaluation.submit({**payload, "case": case, "iteration": 0})
    task = await store.get_task(task_id)
    backend = DshBackend(settings.default_model, settings.dsh_version)

    class Headless:
        async def exec(self, _id, argv, *, stdin, **kwargs):
            assert argv[argv.index("--profile") + 1] == "headless"
            patch, prompt = stdin.split(b"\n", 1)
            assert json.loads(patch)[-1]["insert"][0]["config"]["guidance"] == guidance["system_prompt"]
            assert prompt.decode() == task.spec["instruction"]
            for event in (
                {"type": "session", "sessionId": "session-fixture"},
                {"type": "final", "sessionId": "session-fixture", "text": "", "reason": {"kind": "completed"}},
            ):
                yield ExecOutput(stream="stdout", text=json.dumps(event))
            yield ExecExit(exit_code=0)

    sandbox = Headless()
    kernel = KernelActivities(settings=settings, store=store, sandbox=sandbox, agent=backend, objects=objects)
    ctx = kernel._agent_ctx(task_id, CodingSpec.model_validate(task.spec))
    ctx.model_base_url, ctx.model_api_key = "http://gateway", "fixture-virtual"
    events = [event async for event in backend.run_turn(
        sandbox, "fixture", ctx, task.spec["instruction"], session_id=None, timeout_s=10,
    )]
    assert ctx.agent_version == settings.dsh_version and task.spec["use_champion"] is False
    assert events[-1].type == "turn_completed"


async def test_champions_and_rollouts_with_the_same_version_and_model_are_isolated(api_store):
    platform, team = (api_store.for_tenant(name) for name in ("eval", "team"))
    opencode, parent, dsh = uuid4(), uuid4(), uuid4()
    version, model = "0.2.2-rc.5", "deepseek-flash"
    base = {"agent_version": version, "model": model, "overlay": {}, "overlay_sha256": "hash", "metrics": {}}
    await asyncio.gather(
        platform.activate_champion(str(opencode), base),
        platform.activate_champion(str(parent), {**base, "agent_backend": "dsh"}),
    )
    await platform.activate_champion(str(dsh), {**base, "agent_backend": "dsh", "parent_champion_id": parent})
    assert (await platform.get_active_champion(version, model))["id"] == opencode
    assert (await platform.get_active_champion(version, model, agent_backend="dsh"))["id"] == dsh
    with pytest.raises(NotFoundError):
        await team.put_champion_rollout(version, model, dsh, 100, "operator")
    await team.put_champion_rollout(version, model, opencode, 100, "operator")
    await team.put_champion_rollout(version, model, dsh, 100, "operator", agent_backend="dsh")
    assert (await select_champion(api_store, uuid4(), "team", version, model, None))["id"] == opencode
    assert (await select_champion(api_store, uuid4(), "team", version, model, None, agent_backend="dsh"))["id"] == dsh
    assert (await platform.rollback_champion(dsh, "operator"))["id"] == parent
    rollouts = {row["agent_backend"]: row["champion_id"] for row in await team.list_champion_rollouts()}
    assert rollouts == {"opencode": opencode, "dsh": parent}
    with pytest.raises(ConflictError):
        cross_backend = uuid4()
        await platform.activate_champion(
            str(cross_backend), {**base, "agent_backend": "dsh", "parent_champion_id": opencode},
        )
        await platform.rollback_champion(cross_backend, "operator")


async def test_0025_backfills_existing_rows_and_preserves_rls(store, api_store):
    migration = import_module("sca.db.migrations.versions.0025_evolution_backend")
    champion_id, schedule_id = uuid4(), uuid4()

    def migrate(conn):
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
            conn.execute(text("""INSERT INTO champions
                (id, tenant_id, agent_version, model, overlay, overlay_sha256, metrics, active)
                VALUES (:id, 'eval', 'v', 'm', '{}', 'hash', '{}', true)"""), {"id": champion_id})
            conn.execute(text("""INSERT INTO champion_rollouts
                (tenant_id, agent_version, model, champion_id, percent, updated_by)
                VALUES ('team', 'v', 'm', :id, 30, 'operator')"""), {"id": champion_id})
            conn.execute(text("""INSERT INTO evolution_schedules
                (id, tenant_id, suite, agent_version, model, interval_hours, config, threshold)
                VALUES (:id, 'eval', 'suite', 'v', 'm', 24, '{}', 0.8)"""), {"id": schedule_id})
            migration.upgrade()

    async with store.sessionmaker.kw["bind"].begin() as conn:
        await conn.run_sync(migrate)
    platform, team, other = (api_store.for_tenant(name) for name in ("eval", "team", "other"))
    assert (await platform.get_active_champion("v", "m"))["agent_backend"] == "opencode"
    assert (await team.get_champion_rollout("v", "m"))["agent_backend"] == "opencode"
    assert (await platform.get_evolution_schedule(schedule_id))["agent_backend"] == "opencode"
    async with other._transaction() as session:
        for table in (champions, champion_rollouts, evolution_schedules):
            assert not (await session.execute(select(table))).all()
