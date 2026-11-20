import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from sca.config import Settings
from sca.core.errors import ConflictError, NotFoundError
from sca.core.models import TaskState
from sca.maintenance import MaintenanceActivities, cleanup_finished_cache

CACHE = Path("agent_home/.cache/opencode/bin")


def make_cache(directory):
    path = directory / CACHE
    path.mkdir(parents=True)
    (path / "binary").write_bytes(b"cache")
    return path


async def test_retention_expires_only_old_terminal_directories(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    root = settings.tasks_dir()
    now = datetime.now(UTC)
    tasks, directories = {}, {}
    for name, state, days in (
        ("old", TaskState.FAILED, 15), ("recent", TaskState.SUCCEEDED, 1),
        ("running", TaskState.RUNNING, 20), ("unknown", None, 20),
    ):
        task_id = uuid4()
        directories[name] = root / str(task_id)
        make_cache(directories[name])
        if state:
            tasks[str(task_id)] = SimpleNamespace(state=state, updated_at=now - timedelta(days=days))
        (directories[name] / "keep-alongside-cache").touch()
    artifact = tmp_path / "objects" / "published.patch"
    artifact.parent.mkdir()
    artifact.touch()
    outside = tmp_path / "outside"
    make_cache(outside)
    (root / str(uuid4())).symlink_to(outside, target_is_directory=True)

    async def get_task(task_id):
        if task_id not in tasks:
            raise NotFoundError("missing")
        return tasks[task_id]

    store = SimpleNamespace(get_task=get_task, append_event=AsyncMock())
    maintenance = MaintenanceActivities(settings, store, None, None, None)
    await maintenance.prune_workspaces()
    await maintenance.prune_workspaces()

    assert not directories["old"].exists()
    assert not (directories["recent"] / CACHE).exists()
    assert (directories["recent"] / "keep-alongside-cache").exists()
    for directory in (directories["running"], directories["unknown"], outside):
        assert (directory / CACHE / "binary").is_file()
    assert artifact.is_file()
    assert {call.args[2]["target"] for call in store.append_event.await_args_list} == {
        "task_directory", "opencode_cache",
    }
    assert store.append_event.await_count == 2
    assert all(call.args[2]["freed_bytes"] > 0 for call in store.append_event.await_args_list)


async def test_cache_cleanup_skips_symlink_ancestors_and_cleans_child_cache(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    task_id = uuid4()
    task_dir = settings.task_dir(str(task_id))
    child_cache = make_cache(task_dir / "sub" / "child")
    outside = tmp_path / "outside"
    outside_cache = make_cache(outside)
    (task_dir / "agent_home").symlink_to(outside / "agent_home", target_is_directory=True)
    store = SimpleNamespace(append_event=AsyncMock())
    await cleanup_finished_cache(settings, store, task_id)
    assert outside_cache.exists()
    assert not child_cache.exists()
    store.append_event.assert_awaited_once()
    settings.cache_cleanup = False
    child_cache = make_cache(task_dir / "sub" / "child")
    await cleanup_finished_cache(settings, store, task_id)
    assert child_cache.exists()


async def test_foreign_ownership_uses_confined_helper_and_archived_tasks_log(
    tmp_path, monkeypatch, caplog,
):
    settings = Settings(_env_file=None, data_dir=tmp_path)
    task_id = uuid4()
    task_dir = settings.task_dir(str(task_id))
    path = make_cache(task_dir)
    monkeypatch.setattr("sca.workspace.layers.disk_bytes", AsyncMock(side_effect=PermissionError))

    async def remove(*argv):
        assert f"{task_dir}:/cleanup" in argv
        assert "--network=none" in argv and "--cap-drop=ALL" in argv
        assert argv[-1] == CACHE.as_posix()
        shutil.rmtree(path)
        return "123"

    helper = AsyncMock(side_effect=remove)
    monkeypatch.setattr("sca.workspace.layers.command", helper)
    store = SimpleNamespace(append_event=AsyncMock(side_effect=ConflictError("Events are archived")))
    with caplog.at_level(logging.INFO):
        await cleanup_finished_cache(settings, store, task_id)
    helper.assert_awaited_once()
    assert not path.exists()
    assert "target=opencode_cache freed_bytes=123" in caplog.text
