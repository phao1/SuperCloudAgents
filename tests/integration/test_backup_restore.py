import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from sca.config import get_settings
from sca.core.models import TaskState
from sca.db.engine import make_sessionmaker
from sca.db.repo import Store
from sca.db.schema import metadata
from sca.storage.local import LocalObjectStore, key_for

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


async def run(*args: str, env: dict[str, str] | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    assert proc.returncode == 0, stderr.decode()
    return stdout.decode()


async def test_backup_restore(tmp_path):
    container = os.environ.get("SCA_POSTGRES_CONTAINER", "sca-postgres-1")
    docker = os.environ.get("DOCKER_BIN", "docker")
    try:
        await run(docker, "exec", container, "true")
    except (OSError, AssertionError, TimeoutError) as exc:
        pytest.skip(f"Docker Postgres unavailable: {exc}")

    source = "backup_source_" + uuid4().hex
    target = "backup_target_" + uuid4().hex
    url = make_url(get_settings().database_url)
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT")
    source_engine = create_async_engine(url.set(database=source))
    target_engine = create_async_engine(url.set(database=target))
    created_source = False
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{source}"'))
            created_source = True
        async with source_engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            # Migrations seed plans; create_all only builds tables.
            await conn.execute(text("INSERT INTO plans (name) VALUES ('enterprise')"))
        store = Store(make_sessionmaker(source_engine))
        objects = LocalObjectStore(tmp_path / "source-objects")
        task, _ = await store.create_task(
            tenant_id="default",
            kind="coding",
            spec={"instruction": "historical task"},
            idempotency_key=None,
        )
        await store.append_event(task.id, "agent.message", {"text": "saved"}, "message")
        await store.set_state(task.id, TaskState.CANCELLED, "finished")
        payload = b"restored artifact\x00\xff"
        key = key_for(task.id, "changes.patch")
        uri = await objects.put(key, payload)
        await store.add_artifact(task.id, "changes.patch", "text/plain", uri, len(payload))
        expected_task = await store.get_task(task.id)
        expected_events = await store.list_events(task.id)
        backup = tmp_path / "backup"
        restored_objects = tmp_path / "restored-objects"
        env = {
            **os.environ,
            "SCA_BACKUP_DATABASE": source,
            "SCA_POSTGRES_CONTAINER": container,
            "SCA_POSTGRES_USER": url.username or "sca",
            "SCA_OBJECTS_DIR": str(objects.root),
            "SCA_RESTORE_OBJECTS_DIR": str(restored_objects),
        }
        assert "Backed up" in await run(
            "bash", str(ROOT / "deploy/backup.sh"), str(backup), env=env
        )
        manifest = (backup / "manifest.txt").read_text()
        assert f"database={source}" in manifest
        assert f"database_bytes={(backup / 'database.dump').stat().st_size}" in manifest
        assert "Restored" in await run(
            "bash", str(ROOT / "deploy/restore.sh"), str(backup), target, env=env
        )
        restored = Store(make_sessionmaker(target_engine))
        assert await restored.get_task(task.id) == expected_task
        events = await restored.list_events(task.id)
        assert events == expected_events
        assert [event.seq for event in events] == [1, 2, 3]
        artifact = await restored.get_artifact(task.id, "changes.patch")
        assert artifact.size_bytes == len(payload)
        assert (
            await LocalObjectStore(restored_objects).get(key_for(artifact.task_id, artifact.name))
            == payload
        )
        # An existing database must survive a second restore attempt unchanged.
        env["SCA_RESTORE_OBJECTS_DIR"] = str(tmp_path / "another-target")
        with pytest.raises(AssertionError):
            await run("bash", str(ROOT / "deploy/restore.sh"), str(backup), target, env=env)
        assert await restored.get_task(task.id) == expected_task
    finally:
        await source_engine.dispose()
        await target_engine.dispose()
        async with admin.connect() as conn:
            # Only UUID-named scratch databases can reach these statements.
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)'))
            if created_source:
                await conn.execute(text(f'DROP DATABASE "{source}" WITH (FORCE)'))
        await admin.dispose()
