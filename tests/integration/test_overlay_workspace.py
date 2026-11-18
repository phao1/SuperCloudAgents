import os
import shutil
from uuid import uuid4

import pytest

from sca.workspace.layers import LayerStore, command, disk_bytes, permissions
from sca.workspace.overlay import OverlayWorkspace

pytestmark = [pytest.mark.integration, pytest.mark.docker,
              pytest.mark.skipif(os.environ.get("SCA_TEST_OVERLAY") != "1",
                                 reason="Set SCA_TEST_OVERLAY=1 for Docker overlay")]


async def test_overlay_isolation_commit_push_restore_cleanup(tmp_path):
    source = tmp_path / "source"
    await command("git", "init", "-b", "main", str(source))
    (source / "hello").write_text("before\n")
    (source / "deleted").write_text("delete me\n")
    await command("git", "-C", str(source), "add", ".")
    await command("git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=t@t",
                  "commit", "-m", "initial")
    layers = LayerStore(tmp_path / "layers")
    workspaces = [OverlayWorkspace(tmp_path / "tasks" / uuid4().hex, f"sca/test-{i}",
                                   layers=layers) for i in range(2)]
    try:
        prepared = [await ws.prepare(str(source), "HEAD") for ws in workspaces]
        one, two = workspaces
        assert (one.task_dir / "overlay.layer").read_text() == (
            two.task_dir / "overlay.layer").read_text()
        # Git branch/index/exclude initialization has bounded metadata overhead.
        assert await disk_bytes(two.upper) < 1024 * 1024
        await one.helper(["bash", "-c", ("echo after > hello; rm deleted; "
                          "mkdir -p node_modules; echo cache > node_modules/cache")])
        await one.helper(one.commit_argv("overlay result"))
        commit = await one.fetch()
        patch = await one.diff(prepared[0].base_sha)
        assert "+after" in patch and "deleted file" in patch
        assert await two.helper(["cat", "hello"]) == "before"
        assert await two.helper(["cat", "deleted"]) == "delete me"
        target = tmp_path / "target.git"
        await command("git", "init", "--bare", str(target))
        await one.push(str(target))
        assert await command("git", "--git-dir", str(target), "rev-parse", one.branch) == commit
        snapshot = one.snapshot()
        await one.remove_volume()
        await one.restore_snapshot(snapshot)
        assert await one.helper(["cat", "hello"]) == "after"
        assert await one.helper(["cat", "node_modules/cache"]) == "cache"
        assert await one.helper(["bash", "-c", "test ! -e deleted; echo ok"]) == "ok"
        assert await one.fetch() == commit
    finally:
        for ws in workspaces:
            if (ws.task_dir / "workspace.overlay").exists():
                await ws.cleanup()
                assert ws.volume not in (await command(
                    "docker", "volume", "ls", "--format", "{{.Name}}")).splitlines()
                assert not ws.overlay.exists()
        await layers.prune(0)
        permissions(tmp_path, True)
        shutil.rmtree(tmp_path / "tasks", ignore_errors=True)
