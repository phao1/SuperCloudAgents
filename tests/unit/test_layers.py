import asyncio
import shutil
import time
from pathlib import Path

from sca.workspace.layers import LayerStore, command


def test_keys(tmp_path: Path):
    key = LayerStore.key("https://EXAMPLE.com/r/", "a" * 40)
    assert key == LayerStore.key("https://example.com/r", "a" * 40)
    assert key != LayerStore.key("https://example.com/r", "b" * 40)
    assert key != LayerStore.key("https://example.com/r", "a" * 40, "other")


async def test_copy_lock_pin_prune_and_isolation(tmp_path: Path):
    repo = tmp_path / "repo"
    await command("git", "init", "--initial-branch=main", str(repo))
    (repo / "file").write_text("original")
    await command("git", "-C", str(repo), "add", ".")
    await command("git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=t@t",
                  "commit", "-m", "initial")
    commit = await command("git", "-C", str(repo), "rev-parse", "HEAD")
    store = LayerStore(tmp_path / "layers")
    one, two = tmp_path / "one", tmp_path / "two"
    keys = await asyncio.gather(*(store.copy(repo, commit, str(repo), path)
                                  for path in (one, two)))
    assert keys[0] == keys[1]
    layer = store.root / keys[0]
    assert not (layer / "file").stat().st_mode & 0o222
    assert (one / "file").stat().st_ino != (two / "file").stat().st_ino
    (one / "file").write_text("changed")
    assert (two / "file").read_text() == "original"
    assert (layer / "file").read_text() == "original"
    assert await store.prune(0) > 0
    shutil.rmtree(one)
    assert await store.prune(0) > 0
    shutil.rmtree(two)
    assert await store.prune(24) > 0
    assert layer.stat().st_mtime >= time.time() - 5
    assert await store.prune(0) == 0
    assert not layer.exists()
