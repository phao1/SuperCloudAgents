import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sca.config import Settings
from sca.maintenance import MaintenanceActivities
from sca.storage.local import LocalObjectStore


@pytest.mark.parametrize("state,age,snapshot,corrupt,removed", [
    ("SUCCEEDED", 2, True, False, True),
    ("FAILED", 2, True, False, True),
    ("CANCELLED", 2, True, False, True),
    ("RUNNING", 2, True, False, False),
    ("SUCCEEDED", 0, True, False, False),
    ("SUCCEEDED", 2, False, False, False),
    ("SUCCEEDED", 2, True, True, False),
])
async def test_prune_workspace(tmp_path, state, age, snapshot, corrupt, removed):
    task_id = str(uuid4())
    directory = tmp_path / "tasks" / task_id
    workspace = directory / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "file").touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").touch()
    (workspace / "link").symlink_to(outside)
    (directory / "workspace.ready").touch()
    (directory / "mirror.git").mkdir()
    objects = LocalObjectStore(tmp_path / "objects")
    if snapshot:
        digest = hashlib.sha256(b"snapshot").hexdigest()
        await objects.put(f"tasks/{task_id}/state/blobs/{digest}",
                          b"bad" if corrupt else b"snapshot")
        await objects.put(f"tasks/{task_id}/state/manifest.json", json.dumps({
            "commit": "sha", "sha256": {"mirror.bundle": digest, "agent_home.tar": digest}
        }).encode())
    task = SimpleNamespace(state=state, updated_at=datetime.now(UTC)-timedelta(hours=age),
                           result={"commit": "sha"})
    activities = MaintenanceActivities(Settings(_env_file=None, data_dir=tmp_path),
                                       SimpleNamespace(get_task=AsyncMock(return_value=task)),
                                       objects, None, None)
    if corrupt:
        with pytest.raises(ValueError, match="checksum"):
            await activities.prune_workspaces()
    else:
        await activities.prune_workspaces()
        await activities.prune_workspaces()
    assert workspace.exists() != removed
    assert (directory / "workspace.ready").exists() != removed
    assert (directory / "mirror.git").exists()
    assert (outside / "keep").exists()
