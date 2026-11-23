import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import func, insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.archive import archive_task
from sca.config import Settings
from sca.core.models import TaskState
from sca.db import schema as db
from sca.db.engine import make_sessionmaker
from sca.db.repo import Store
from sca.history.bundle import BundleReader, HistoryError
from sca.history.transfer import _IMPORT_LOCK, STOP_REASON, export_history, import_history
from sca.storage.local import LocalObjectStore, history_artifact_key, key_for

pytestmark = pytest.mark.integration
PRIVATE = "fixture-only-private-value"
NOW = datetime.now(UTC)
OLD = NOW - timedelta(days=60)
PATCH = b"diff --git a/readme.md b/readme.md\n+history\n"


@pytest.fixture
def source_revision():
    return "0025"


@pytest.fixture
async def history_databases(source_revision):
    url = make_url(Settings(_env_file=None).database_url)
    admin = create_async_engine(
        url, isolation_level="AUTOCOMMIT", connect_args={"timeout": 5}, hide_parameters=True,
    )
    created = []
    engines = []
    try:
        try:
            conn = await admin.connect()
        except OSError:
            pytest.skip("Postgres socket unavailable for history-transfer integration tests")
        try:
            for _ in range(2):
                name = "test_history_" + uuid4().hex
                await conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
                created.append(name)
                engines.append(create_async_engine(url.set(database=name), hide_parameters=True))
        finally:
            await conn.close()

        def migrate(sync, revision):
            folder = Path(db.__file__).with_name("migrations")
            scripts = ScriptDirectory(str(folder))
            revision = revision or scripts.get_current_head()
            context = MigrationContext.configure(sync)
            with Operations.context(context):
                for script in reversed(list(scripts.iterate_revisions(revision, "base"))):
                    script.module.upgrade()
                context.stamp(scripts, revision)

        for engine, revision in zip(engines, (source_revision, None), strict=True):
            async with engine.begin() as conn:
                await conn.run_sync(migrate, revision)
        yield tuple(engines)
    finally:
        for engine in engines:
            await engine.dispose()
        if created:
            async with admin.connect() as conn:
                for name in created:
                    await conn.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.dispose()


async def seed_history(engine, objects, *, agent_backend: str | None = "opencode"):
    ids = {name: uuid4() for name in (
        "project", "task", "child", "unrelated", "foreign", "message", "artifact",
        "eval_run", "evolution", "candidate", "champion", "session",
    )}
    root = ids["task"]
    backend = {"agent_backend": agent_backend} if agent_backend is not None else {}
    uri = await objects.put(key_for(root, "result.patch"), PATCH, "text/x-diff")
    await objects.put(f"tasks/{root}/state/agent_home.tar", PRIVATE.encode())
    async with engine.begin() as conn:
        await conn.execute(insert(db.tenants), [
            {"id": "eval", "display_name": "演进历史", "max_active_tasks": 30},
            {"id": "other", "display_name": "其他历史", "max_active_tasks": 4},
        ])
        await conn.execute(insert(db.projects).values(
            id=ids["project"], tenant_id="eval", name="源项目", is_default=True,
            settings={"provider_credentials": {"value": PRIVATE}, "label": "history"},
            created_by="fixture", created_at=OLD, updated_at=OLD,
        ))
        await conn.execute(insert(db.tasks), [
            {
                "id": root, "tenant_id": "eval", "project_id": ids["project"], "kind": "coding",
                "spec": {"subtasks": [{"name": "child"}], "max_tokens": 1000, "api_key": PRIVATE},
                "state": "RUNNING", "result": {"patch_artifact": "result.patch"},
                "created_at": OLD, "updated_at": NOW, "slot_held": True, "idempotency_key": PRIVATE,
                "billing_usage": {"account_id": PRIVATE},
            },
            {
                "id": ids["child"], "tenant_id": "eval", "project_id": ids["project"], "kind": "coding",
                "spec": {"agent_backend": "fake"}, "state": "SUCCEEDED",
                "result": {"parent_task_id": str(root), "subtask": "child"},
                "created_at": OLD, "updated_at": OLD, "slot_held": False, "idempotency_key": None,
                "billing_usage": None,
            },
            {
                "id": ids["unrelated"], "tenant_id": "eval", "project_id": None, "kind": "coding",
                "spec": {}, "state": "FAILED", "result": {}, "created_at": OLD, "updated_at": OLD,
                "slot_held": False, "idempotency_key": None, "billing_usage": None,
            },
            {
                "id": ids["foreign"], "tenant_id": "other", "project_id": None, "kind": "coding",
                "spec": {}, "state": "QUEUED", "result": {"parent_task_id": str(root)},
                "created_at": NOW, "updated_at": NOW, "slot_held": False, "idempotency_key": None,
                "billing_usage": None,
            },
        ])
        await conn.execute(insert(db.sessions).values(
            id=ids["session"], task_id=root, tenant_id="eval", backend="fake",
        ))
        await conn.execute(insert(db.events), [
            {"task_id": root, "tenant_id": "eval", "seq": 1, "type": "task.state_changed",
             "payload": {"from": "QUEUED", "to": "RUNNING"}, "created_at": OLD},
            {"task_id": root, "tenant_id": "eval", "seq": 2, "type": "task.child_started",
             "payload": {"child_task_id": str(ids["child"])}, "created_at": OLD},
            {"task_id": root, "tenant_id": "eval", "seq": 3, "type": "user.message",
             "payload": {"message_id": str(ids["message"]), "text": "继续", "mode": "queue"},
             "created_at": OLD},
            {"task_id": ids["child"], "tenant_id": "eval", "seq": 1, "type": "agent.turn_started",
             "payload": {"turn": 1, "virtual_key": PRIVATE}, "created_at": OLD},
        ])
        await conn.execute(insert(db.task_messages).values(
            id=ids["message"], task_id=root, tenant_id="eval", seq=1, mode="queue",
            status="applied", text="继续", actor="fixture", turn=2,
        ))
        await conn.execute(insert(db.artifacts).values(
            id=ids["artifact"], task_id=root, tenant_id="eval", name="result.patch",
            uri=uri, size_bytes=len(PATCH), content_type="text/x-diff",
        ))
        await conn.execute(insert(db.eval_runs).values(
            id=ids["eval_run"], tenant_id="eval", suite="code", suite_hash="suite-sha",
            variant={"evolution_id": str(ids["evolution"]), "split": "dev"}, created_by="fixture",
            status="running", created_at=OLD,
        ))
        await conn.execute(insert(db.eval_results).values(
            run_id=ids["eval_run"], tenant_id="eval", task_id=ids["child"], case_id="readme",
            repeat=1, acceptance_passed=True, trajectory_passed=True, verification_passed=True,
            turns=1, tokens=424, cost_usd=Decimal("0.0123456789012"), duration_s=0.3,
        ))
        await conn.execute(insert(db.evolutions).values(
            id=ids["evolution"], tenant_id="eval", status="awaiting_approval", config={"suite": "code"},
            created_by="fixture", best_champion_id=ids["champion"], created_at=OLD, updated_at=NOW,
        ))
        await conn.execute(insert(db.evolution_candidates).values(
            id=ids["candidate"], tenant_id="eval", evolution_id=ids["evolution"], generation=1,
            overlay={"tool_guidance": "Review the patch"}, overlay_sha256="overlay-sha",
            hypothesis="Clearer review", diagnosis={}, static_check={}, decision="promoted",
            dev_run_id=ids["eval_run"], created_at=OLD,
        ))
        await conn.execute(insert(db.champions).values(
            id=ids["champion"], tenant_id="eval", agent_version="fake.1", model="fake",
            overlay={}, overlay_sha256="champion-sha", candidate_id=ids["candidate"],
            active=True, metrics={}, created_at=OLD, **backend,
        ))
        await conn.execute(insert(db.evolution_events).values(
            tenant_id="eval", type="rollout.updated", actor="fixture",
            payload={"champion_id": str(ids["champion"])}, created_at=NOW,
        ))
        await conn.execute(insert(db.champion_rollouts).inline().values(
            tenant_id="eval", agent_version="fake.1", model="fake", champion_id=ids["champion"],
            percent=100, updated_by="fixture", **backend,
        ))
        await conn.execute(insert(db.api_tokens).values(
            id=uuid4(), tenant_id="eval", name="fixture", token_hash=PRIVATE,
            expires_at=NOW + timedelta(days=1), created_by="fixture",
        ))
        await conn.execute(insert(db.billing_customers).values(
            tenant_id="eval", stripe_customer_id=PRIVATE,
        ))
    store = Store(make_sessionmaker(engine), objects=objects)
    await archive_task(store, objects, ids["child"])
    return ids


@pytest.mark.parametrize("source_revision", ["0024", "0025"])
async def test_history_round_trip_and_existing_history_surfaces(
    history_databases, source_revision, tmp_path, monkeypatch,
):
    linux, windows = history_databases
    source = LocalObjectStore(tmp_path / "linux" / "objects")
    target = LocalObjectStore(tmp_path / "windows" / "objects")
    backend = None if source_revision == "0024" else "dsh"
    ids = await seed_history(linux, source, agent_backend=backend)
    path = tmp_path / "history.zip"
    manifest = await export_history(linux, source, path)
    assert manifest.schema_revision == source_revision
    assert manifest.counts["tenants"] == 2
    assert manifest.counts["tasks"] == 4
    assert manifest.counts["objects"] == 2
    with ZipFile(path) as archive:
        BundleReader(archive).verify()
        assert set(archive.namelist()) == set(manifest.files) | {"manifest.json"}
        for name in archive.namelist():
            assert PRIVATE.encode() not in archive.read(name)

    counts = await import_history(windows, target, path, default_quota=9)
    assert counts["objects"] == 2 and counts["tasks"] == 4
    assert not any((await import_history(windows, target, path)).values())
    store = Store(make_sessionmaker(windows), objects=target)
    scoped = store.for_tenant("eval")
    task = await scoped.get_task(ids["task"])
    assert task.state == TaskState.CANCELLED and task.state_reason == STOP_REASON
    assert task.result["history_imported"] and task.idempotency_key is None
    assert task.spec["max_tokens"] == 1000 and "api_key" not in task.spec
    assert task.project_id == ids["project"]
    assert (await scoped.get_project(ids["project"])).settings["history_source_default"] is True
    assert (await scoped.list_task_messages(task.id))[0].id == ids["message"]
    assert (await scoped.list_events(task.id))[-1].payload["to"] == "CANCELLED"
    child = await scoped.get_task(ids["child"])
    assert child.state == TaskState.SUCCEEDED
    assert (await scoped.list_events(child.id))[0].payload == {"turn": 1}
    assert await store.acquire_slot(str(task.id), 5) == {"granted": False, "terminal": True}
    run = await scoped.get_eval_run(str(ids["eval_run"]))
    assert run["status"] == "stopped" and run["reason"] == STOP_REASON
    assert run["results"][0]["task_id"] == child.id
    assert run["results"][0]["cost_usd"] == Decimal("0.0123456789012")
    evolution = await scoped.get_evolution(str(ids["evolution"]))
    assert evolution["status"] == "stopped" and evolution["reason"] == STOP_REASON
    assert evolution["candidates"][0]["id"] == ids["candidate"]
    assert evolution["best_champion_id"] == ids["champion"]
    champion = await scoped.get_champion(ids["champion"])
    assert not champion["active"] and champion["metrics"]["history_source_active"]
    assert champion["agent_backend"] == (backend or "opencode")
    artifact = await scoped.get_artifact(task.id, "result.patch")
    assert artifact.uri == target._path(history_artifact_key(task.id, artifact.name)).as_uri()
    assert await target.get(history_artifact_key(task.id, artifact.name)) == PATCH

    await store.usage_backfill()
    async with windows.connect() as conn:
        for table in (db.api_tokens, db.billing_customers, db.usage_daily, db.champion_rollouts):
            assert await conn.scalar(select(func.count()).select_from(table)) == 0
        assert await conn.scalar(select(db.tenants.c.max_active_tasks).where(db.tenants.c.id == "eval")) == 9
        assert await conn.scalar(select(func.count()).select_from(db.evolution_events)) == 1
        assert await conn.scalar(select(db.sessions.c.id)) == ids["session"]
        assert await conn.scalar(select(db.artifacts.c.id)) == ids["artifact"]

    settings = Settings(_env_file=None, data_dir=tmp_path / "windows", guest_view="off")
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    orchestrator = SimpleNamespace(start_task=AsyncMock(), resume_task=AsyncMock())
    app = create_app(
        store=store, orchestrator=orchestrator, object_store=target,
        authenticator=StaticTokenAuthenticator({"fixture": "eval:admin", "other": "other:viewer"}),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer fixture"}
        path = f"/v1/tasks/{task.id}"
        dag = (await client.get(path + "/dag", headers=headers)).json()
        assert {row["task_id"] for row in dag["nodes"]} == {str(task.id), str(child.id)}
        assert any(node["kind"] == "interjection" for node in dag["nodes"])
        assert (await client.get(path + "/artifacts/result.patch", headers=headers)).content == PATCH
        assert (await client.post(path + "/resume", headers=headers)).status_code == 409
        assert (await client.get(path, headers={"Authorization": "Bearer other"})).status_code == 404
    orchestrator.start_task.assert_not_awaited()
    orchestrator.resume_task.assert_not_awaited()


def run_history_export(
    engine: AsyncEngine, data_dir: Path, bundle: Path, *flags: str,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "SCA_DATABASE_URL": engine.url.render_as_string(hide_password=False),
        "SCA_DATA_DIR": str(data_dir),
        "SCA_OBJECT_STORE": "local",
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        [sys.executable, "-m", "sca.history", "export", "--out", str(bundle), *flags],
        env=env, capture_output=True, encoding="utf-8", check=False, timeout=30,
    )


async def test_history_round_trip_with_deleted_artifact_blob(history_databases, tmp_path, monkeypatch):
    linux, windows = history_databases
    data_dir = tmp_path / "linux"
    source = LocalObjectStore(data_dir / "objects")
    target = LocalObjectStore(tmp_path / "windows" / "objects")
    ids = await seed_history(linux, source)
    task_id, name = ids["task"], "result.patch"
    await source.delete(key_for(task_id, name))
    path = tmp_path / "history.zip"

    strict = run_history_export(linux, data_dir, path, "--strict")
    assert strict.returncode == 1 and not path.exists()
    assert "严格模式下发现缺失的制品文件" in strict.stderr
    completed = run_history_export(linux, data_dir, path)
    assert completed.returncode == 0
    assert "正在导出历史" in completed.stderr and name in completed.stdout
    assert "已复制 1 个对象，缺失 1 个对象" in completed.stdout
    with ZipFile(path) as archive:
        bundle = BundleReader(archive)
        bundle.verify()
        assert bundle.manifest.counts["artifacts"] == bundle.manifest.counts["missing_objects"] == 1
        assert [obj.model_dump() for obj in bundle.manifest.missing_objects] == [
            {"task_id": task_id, "name": name},
        ]
        assert f"objects/artifacts/{ids['artifact']}" not in archive.namelist()

    counts = await import_history(windows, target, path)
    assert counts["artifacts"] == counts["objects"] == 1
    assert await target.list(f"tasks/{task_id}/") == []
    async with windows.connect() as conn:
        assert await conn.scalar(select(db.artifacts.c.id)) == ids["artifact"]

    settings = Settings(_env_file=None, data_dir=tmp_path / "windows", guest_view="local", guest_tenant="eval")
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    app = create_app(
        store=Store(make_sessionmaker(windows), objects=target),
        orchestrator=SimpleNamespace(), object_store=target,
        authenticator=StaticTokenAuthenticator({"fixture": "eval:admin", "other": "other:viewer"}),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        reason = "文件缺失（源机器上已清理）"
        auth = {"Authorization": "Bearer fixture"}
        assert (await client.get("/portal")).status_code == 200
        root = f"/v1/tasks/{task_id}/artifacts"
        for headers in ({}, auth):
            response = await client.get(root, headers=headers)
            assert response.status_code == 200
            artifact, = response.json()
            assert artifact["name"] == name and artifact["missing_reason"] == reason
            download = await client.get(f"{root}/{name}", headers=headers)
            assert download.status_code == 410
            assert download.json() == {"error": {"code": "artifact_missing", "message": reason}}
        library = await client.get(f"/v1/projects/{ids['project']}/library", headers=auth)
        assert library.json()["artifacts"][0]["missing_reason"] == reason
        assert (await client.get(f"{root}/{name}", headers={"Authorization": "Bearer other"})).status_code == 404


@pytest.mark.parametrize("source_revision", ["0023"])
async def test_history_export_reports_source_upgrade_in_chinese(history_databases, tmp_path):
    source, _ = history_databases
    path = tmp_path / "history.zip"
    completed = run_history_export(source, tmp_path, path)
    assert completed.returncode == 1 and not path.exists()
    assert "源库版本 0023，需先升级到 0024" in completed.stderr
    assert "Traceback" not in completed.stderr


async def test_history_filters_keep_older_dependencies(history_databases, tmp_path):
    source, _ = history_databases
    objects = LocalObjectStore(tmp_path / "objects")
    ids = await seed_history(source, objects)
    path = tmp_path / "filtered.zip"
    manifest = await export_history(
        source, objects, path, tenants=("eval", "absent"), since=NOW - timedelta(days=1),
    )
    assert manifest.counts["tenants"] == 1
    assert manifest.counts["tasks"] == 2
    assert manifest.counts["projects"] == 1
    assert manifest.counts["eval_runs"] == manifest.counts["champions"] == 1
    with ZipFile(path) as archive:
        data = archive.read("tables/tasks.jsonl")
        assert str(ids["child"]).encode() in data and str(ids["task"]).encode() in data
        assert str(ids["unrelated"]).encode() not in data and str(ids["foreign"]).encode() not in data


async def test_history_import_lock_and_live_collision(history_databases, tmp_path):
    source, target = history_databases
    objects = LocalObjectStore(tmp_path / "objects")
    ids = await seed_history(source, objects)
    path = tmp_path / "filtered.zip"
    await export_history(source, objects, path)
    target_objects = LocalObjectStore(tmp_path / "target")
    live = Store(make_sessionmaker(target), objects=target_objects)
    async with target.begin() as conn:
        await conn.execute(insert(db.tasks).values(
            id=ids["task"], kind="coding", tenant_id="eval", spec={}, state="RUNNING",
        ))
        assert await conn.scalar(select(func.pg_try_advisory_xact_lock(_IMPORT_LOCK)))
        with pytest.raises(HistoryError, match="Another history import"):
            await import_history(target, target_objects, path)
    await import_history(target, target_objects, path)
    assert (await live.get_task(ids["task"])).state == TaskState.RUNNING
    assert await live.list_events(ids["task"]) == []
    assert await live.list_artifacts(ids["task"]) == []
    assert await target_objects.list(f"tasks/{ids['task']}/") == []


async def test_unknown_archive_schema_is_rejected_before_import(history_databases, tmp_path):
    source, target = history_databases
    objects = LocalObjectStore(tmp_path / "objects")
    await seed_history(source, objects)
    path = tmp_path / "portable.zip"
    manifest = await export_history(source, objects, path)
    changed = tmp_path / "newer.zip"
    with ZipFile(path) as original, ZipFile(changed, "w") as modified:
        for name in original.namelist():
            modified.writestr(
                name, manifest.model_copy(update={"schema_revision": "9999"}).model_dump_json()
                if name == "manifest.json" else original.read(name),
            )
    with pytest.raises(HistoryError, match="schema"):
        await import_history(target, objects, changed)
    async with target.connect() as conn:
        assert await conn.scalar(select(db.tasks.c.id)) is None
