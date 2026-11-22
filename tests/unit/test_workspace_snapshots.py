import asyncio
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

from sca.core.errors import WorkspaceError
from sca.workspace.layers import permissions
from sca.workspace.snapshots import SnapshotStore, preparation_key
from sca.workspace.tarsafe import chunks, pack


def key(tenant="one", **changes):
    return preparation_key(**{ "tenant": tenant, "repo_key": "repo", "commit": "abc",
                              "setup": "install", "image": "image", **changes})


def archive(tree: Path):
    stream = io.BytesIO()
    pack(tree, stream)
    stream.seek(0)
    return chunks(stream)


@pytest.mark.parametrize("change", [{"tenant": "two"}, {"repo_key": "other"},
                                   {"commit": "def"}, {"setup": "other"}, {"image": "new"}])
def test_key_identity(change):
    assert key() == key()
    assert key(**change) != key()
    assert preparation_key("a\0b", "c", "d", "e", "f") != preparation_key(
        "a", "b\0c", "d", "e", "f")


async def test_publish_fork_isolation_and_gc(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "dependency").write_text("prepared")
    (source / "link").symlink_to("dependency")
    (source / ".sca").mkdir()
    (source / ".sca" / "context").write_text("private")
    store = SnapshotStore(tmp_path / "snapshots")
    metadata = await store.publish(key(), archive(source), base_commit="abc", saved_seconds=10)
    assert metadata["base_commit"] == "abc"
    assert not (store.root / metadata["id"] / ".sca").exists()
    assert (store.root / metadata["id"] / "dependency").read_text() == "prepared"
    assert await store.fork(key("two"), tmp_path / "wrong") is None
    first, second = tmp_path / "first", tmp_path / "second"
    await asyncio.gather(store.fork(key(), first), store.fork(key(), second))
    assert (first / "dependency").stat().st_ino != (second / "dependency").stat().st_ino
    (first / "dependency").write_text("changed")
    assert (second / "dependency").read_text() == "prepared"
    assert (second / "link").read_text() == "prepared"
    assert await store.prune(0) == metadata["size_bytes"]
    import shutil

    shutil.rmtree(first)
    shutil.rmtree(second)
    assert await store.prune(0) == 0
    assert not (store.root / metadata["id"]).exists()


async def test_content_address_and_atomic_failure(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_text("value")
    one, two = SnapshotStore(tmp_path / "one"), SnapshotStore(tmp_path / "two")
    a = await one.publish(key(), archive(source), base_commit="abc", saved_seconds=1)
    b = await two.publish(key(), archive(source), base_commit="abc", saved_seconds=2)
    assert a["id"] == b["id"]
    (source / "data").write_text("different")
    three = SnapshotStore(tmp_path / "three")
    c = await three.publish(key(), archive(source), base_commit="abc", saved_seconds=1)
    assert a["id"] != c["id"]
    tiny = SnapshotStore(tmp_path / "tiny", max_bytes=1)
    with pytest.raises(WorkspaceError, match="size limit"):
        await tiny.publish(key(), archive(source), base_commit="abc", saved_seconds=1)
    assert not list(tiny.root.glob("*.json"))
    assert not list(tiny.root.glob(".prepare-*"))
    for store in (one, two, three):
        await store.prune(0)


async def test_reject_escape_archive(tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        member = tarfile.TarInfo("../escape")
        member.size = 1
        tar.addfile(member, io.BytesIO(b"x"))
    stream.seek(0)
    store = SnapshotStore(tmp_path / "snapshots")
    with pytest.raises(tarfile.TarError):
        await store.publish(key(), chunks(stream), base_commit="abc", saved_seconds=0)
    assert not list(store.root.glob("*.json"))
    assert not (tmp_path / "escape").exists()


async def test_overlay_pin_and_age(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_text("prepared")
    store = SnapshotStore(tmp_path / "snapshots")
    metadata = await store.publish(key(), archive(source), base_commit="abc", saved_seconds=1)
    assert await store.prune(1) == metadata["size_bytes"]
    task = tmp_path / "task"
    task.mkdir()
    await store.fork(key(), task / "overlay", overlay=True)
    (task / "workspace.overlay").write_text(json.dumps({"lower": metadata["id"]}))
    assert (task / "overlay").is_dir()
    assert await store.prune(0) == metadata["size_bytes"]
    (task / "workspace.overlay").unlink()
    (task / "overlay").rmdir()
    (task / "state.manifest.json").write_text("{}")
    assert await store.prune(0) == metadata["size_bytes"]
    (task / "state.manifest.json").unlink()
    index = store.root / f"{key()}.json"
    os.utime(index, (0, 0))
    assert await store.prune(1) == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Docker OverlayFS uses Linux host paths")
async def test_overlay_lower_allows_only_managed_roots(tmp_path, monkeypatch):
    from sca.workspace.overlay import OverlayWorkspace

    ws = OverlayWorkspace(tmp_path / "tasks" / "one", "branch")
    ws.task_dir.mkdir(parents=True)
    marker = ws.task_dir / "workspace.overlay"
    marker.write_text(json.dumps({"lower": str(tmp_path / "snapshots" / ("a" * 64))}))
    calls = []

    async def command(*args):
        calls.append(args)
        return ""

    monkeypatch.setattr("sca.workspace.overlay.command", command)
    await ws.ensure_volume()
    assert any("lowerdir=" in arg and "/snapshots/" in arg for arg in calls[0])
    marker.write_text(json.dumps({"lower": str(tmp_path / "unsafe" / ("a" * 64))}))
    with pytest.raises(WorkspaceError, match="lower layer"):
        await ws.ensure_volume()
    permissions(tmp_path, True)


async def test_gc_cleans_interrupted_publication(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    for name in ("a" * 64, ".prepare-interrupted"):
        orphan = store.root / name
        orphan.mkdir()
        (orphan / "data").write_text("unused")
    assert await store.prune(0) == 0
    assert sorted(p.name for p in store.root.iterdir()) == ["locks"]


async def test_expanded_archive_limit(tmp_path):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        member = tarfile.TarInfo("huge")
        member.size = 30000
        tar.addfile(member, io.BytesIO(b"x" * member.size))
    stream.seek(0)
    store = SnapshotStore(tmp_path / "snapshots", max_bytes=10000)
    with pytest.raises(ValueError, match="expanded size limit"):
        await store.publish(key(), chunks(stream), base_commit="abc", saved_seconds=1)
    assert not list(store.root.glob("*.json"))


async def test_concurrent_publication_has_one_identity(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_text("prepared")
    store = SnapshotStore(tmp_path / "snapshots")
    results = await asyncio.gather(*[
        store.publish(key(), archive(source), base_commit="abc", saved_seconds=1)
        for _ in range(3)])
    assert len({result["id"] for result in results}) == 1
    assert len(list(store.root.glob("*.json"))) == 1
    await store.prune(0)


async def test_overlay_rebuild_references_prepared_snapshot(tmp_path):
    from unittest.mock import AsyncMock

    from sca.workspace.overlay import OverlayWorkspace

    source = tmp_path / "source"
    source.mkdir()
    (source / "dependency").write_text("installed")
    store = SnapshotStore(tmp_path / "snapshots")
    metadata = await store.publish(key(), archive(source), base_commit="abc", saved_seconds=1)
    ws = OverlayWorkspace(tmp_path / "tasks" / "recovery", "sca/recovered")
    ws.task_dir.mkdir(parents=True)
    (ws.task_dir / "layer-source.json").write_text(json.dumps({
        "repo_key": "repo", "tenant_id": "one", "snapshot_key": key()}))
    ws.ensure_volume = AsyncMock()
    ws.helper = AsyncMock(return_value="")
    ws.layers.copy = AsyncMock(side_effect=AssertionError("must use prepared snapshot"))
    await ws.build_workspace("abc")
    lower = json.loads((ws.task_dir / "workspace.overlay").read_text())["lower"]
    assert lower == str(store.root / metadata["id"])
    assert (ws.task_dir / "overlay.snapshot").read_text() == metadata["id"]
    assert await store.prune(0) == metadata["size_bytes"]
    (ws.task_dir / "workspace.overlay").unlink()
    import shutil

    shutil.rmtree(ws.overlay)
    await store.prune(0)
