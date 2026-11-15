import json
from pathlib import Path

import pytest
from test_workspace import source_repo

from sca.core.errors import WorkspaceError
from sca.storage.local import LocalObjectStore
from sca.workspace.git import GitWorkspace
from sca.workspace.state import TaskStateStore

pytestmark = pytest.mark.integration


async def snapshot(tmp_path: Path):
    source = await source_repo(tmp_path / "source")
    ws = GitWorkspace(tmp_path / "worker-a", "sca/test")
    prepared = await ws.prepare(str(source), "main")
    commit = await ws.fetch()
    home = ws.task_dir / "agent_home"
    home.mkdir()
    (home / "session").write_bytes(b"session\x00\xff")
    (home / "link").symlink_to("session")
    (home / "auth.json").write_text('{"secret": "provider credential"}')
    objects = LocalObjectStore(tmp_path / "objects")
    state = TaskStateStore(objects, "task")
    await state.save(ws, 1, commit)
    return ws, prepared, state


async def test_save_restore_all_refs_workspace_and_home(tmp_path, store):
    ws, prepared, state = await snapshot(tmp_path)
    other = GitWorkspace(tmp_path / "worker-b", ws.branch)
    assert await state.restore(other)
    for args in (("show-ref",), ("rev-parse", "HEAD")):
        assert await ws._git("--git-dir", str(ws.mirror), *args) == await other._git(
            "--git-dir", str(other.mirror), *args
        )
    assert (other.workspace / "hello").read_text() == "before\n"
    assert (other.task_dir / "workspace.ready").read_text() == prepared.base_sha
    assert (other.task_dir / "agent_home/session").read_bytes() == b"session\x00\xff"
    assert (other.task_dir / "agent_home/link").is_symlink()
    # A provider credential copied into the agent home is never persisted.
    assert not (other.task_dir / "agent_home/auth.json").exists()
    for blob in (tmp_path / "objects").rglob("*"):
        assert not blob.is_file() or b"provider credential" not in blob.read_bytes()
    assert not await state.restore(other)


async def test_checksum_mismatch_refused(tmp_path, store):
    ws, _, state = await snapshot(tmp_path)
    manifest = await state.manifest()
    key = f"{state.prefix}/blobs/{manifest['sha256']['mirror.bundle']}"
    await state.objects.put(key, b"corrupt")
    with pytest.raises(WorkspaceError, match="checksum"):
        await state.restore(GitWorkspace(tmp_path / "worker-b", ws.branch))
    assert not (tmp_path / "worker-b/mirror.git").exists()


async def test_partial_save_preserves_previous_snapshot(tmp_path, store):
    ws, _, state = await snapshot(tmp_path)
    previous = await state.manifest()
    (ws.task_dir / "agent_home/session").write_text("new session")
    original = state.objects.put

    async def interrupted(key, data, content_type="application/octet-stream"):
        if key.endswith("manifest.json"):
            raise OSError("worker died")
        return await original(key, data, content_type)

    state.objects.put = interrupted
    with pytest.raises(OSError, match="worker died"):
        await state.save(ws, 2, previous["commit"])
    assert await state.manifest() == previous
    other = GitWorkspace(tmp_path / "worker-b", ws.branch)
    assert await state.restore(other)
    assert (other.task_dir / "agent_home/session").read_bytes() == b"session\x00\xff"
    assert json.loads((other.task_dir / "state.manifest.json").read_bytes())["turn"] == 1


async def test_manifest_checksum_mismatch_refused(tmp_path, store):
    ws, _, state = await snapshot(tmp_path)
    manifest = await state.manifest()
    manifest["sha256"]["agent_home.tar"] = "0" * 64
    await state.objects.put(f"{state.prefix}/manifest.json", json.dumps(manifest).encode())
    with pytest.raises(WorkspaceError, match="checksum"):
        await state.restore(GitWorkspace(tmp_path / "worker-b", ws.branch))


async def test_stale_local_turn_restored(tmp_path, store):
    ws, _, state = await snapshot(tmp_path)
    other = GitWorkspace(tmp_path / "worker-b", ws.branch)
    assert await state.restore(other)
    (ws.task_dir / "agent_home/session").write_bytes(b"next turn")
    await state.save(ws, 2, (await state.manifest())["commit"])
    assert await state.restore(other)
    assert (other.task_dir / "agent_home/session").read_bytes() == b"next turn"
    assert json.loads((other.task_dir / "state.manifest.json").read_bytes())["turn"] == 2
