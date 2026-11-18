"""Explicit opt-in for Docker archive shape and real overlay copy-on-write."""
import json
import os
from uuid import uuid4

import pytest

from sca.core.models import ExecExit, Mount, SandboxSpec
from sca.sandbox.docker import DockerSandboxProvider
from sca.workspace.overlay import OverlayWorkspace
from sca.workspace.snapshots import SnapshotStore, preparation_key
from sca.workspace.transport import MountTransport

pytestmark = [pytest.mark.integration, pytest.mark.docker,
              pytest.mark.skipif(os.environ.get("SCA_TEST_DOCKER_SNAPSHOTS") != "1",
                                 reason="explicit Docker snapshot opt-in required")]


async def test_real_overlay_snapshot_fork(tmp_path):
    provider = DockerSandboxProvider()
    image = os.environ.get("SCA_TEST_DOCKER_IMAGE", "sca-sandbox:latest")
    source = tmp_path / "source"
    source.mkdir()
    (source / "prepared").write_text("original")
    snapshots = SnapshotStore(tmp_path / "snapshots")
    ws = OverlayWorkspace(tmp_path / "tasks" / str(uuid4()), "sca/test", image=image)
    first = second = None
    try:
        first = await provider.create(f"sca-snapshot-test-{uuid4().hex}", SandboxSpec(
            image=image, mounts=[Mount(source=str(source), target="/workspace")],
            network="none", memory_mb=128))
        key = preparation_key("test", "repo", "commit", "true", image)
        metadata = await snapshots.publish(key, provider.download(first, "/workspace/."),
                                           base_commit="commit", saved_seconds=1)
        ws.overlay.mkdir(parents=True)
        for part in ("upper", "work"):
            (ws.overlay / part).mkdir()
        await snapshots.fork(key, ws.overlay, overlay=True)
        (ws.task_dir / "workspace.overlay").write_text(json.dumps({
            "volume": ws.volume, "lower": str(snapshots.root / metadata["id"])}))
        await ws.ensure_volume()
        second = await provider.create(f"sca-snapshot-test-{uuid4().hex}", SandboxSpec(
            image=image, mounts=MountTransport(provider).mounts(ws.task_dir),
            network="none", memory_mb=128))
        exits = []
        async for event in provider.exec(second, ["bash", "-lc",
            "test \"$(cat prepared)\" = original && echo changed > prepared"],
            workdir="/workspace", timeout_s=30):
            if isinstance(event, ExecExit):
                exits.append(event.exit_code)
        assert exits == [0]
        assert (source / "prepared").read_text() == "original"
        assert (snapshots.root / metadata["id"] / "prepared").read_text() == "original"
    finally:
        if second:
            await provider.destroy(second)
        if first:
            await provider.destroy(first)
        if (ws.task_dir / "workspace.overlay").exists():
            await ws.cleanup()
        await snapshots.prune(0)
