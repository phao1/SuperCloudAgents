import asyncio
import os
import shutil
from pathlib import Path
from time import perf_counter

import pytest
from test_workspace import git, source_repo

from sca.core.errors import InvalidTaskError
from sca.workspace.cache import PIP_CACHE_ENV, RepoCache, pip_cache_mount
from sca.workspace.git import GitWorkspace

pytestmark = pytest.mark.integration


async def test_fresh_refs_and_independent_mirror(tmp_path: Path):
    source = await source_repo(tmp_path / "source")
    await git("-C", str(source), "branch", "obsolete")
    cache = RepoCache(tmp_path / "cache")
    first = GitWorkspace(tmp_path / "first", "sca/first", cache)
    before = await first.prepare(source.as_uri(), "main")
    (source / "hello").write_text("new\n")
    await git(
        "-C", str(source), "-c", "user.name=test", "-c", "user.email=t@t", "commit", "-am", "new"
    )
    await git("-C", str(source), "branch", "-D", "obsolete")
    second = GitWorkspace(tmp_path / "second", "sca/second", cache)
    after = await second.prepare(source.as_uri(), "main")
    assert after.base_sha != before.base_sha
    assert (second.workspace / "hello").read_text() == "new\n"
    with pytest.raises(InvalidTaskError):
        await GitWorkspace(tmp_path / "deleted", "sca/deleted", cache).prepare(
            source.as_uri(), "obsolete"
        )
    shutil.rmtree(cache.root)
    await git("--git-dir", str(second.mirror), "fsck", "--full")
    await second.fetch()
    assert await second.diff(before.base_sha)
    assert not (second.mirror / "objects/info/alternates").exists()


async def test_concurrent_corruption_and_prune(tmp_path: Path):
    source = await source_repo(tmp_path / "source")
    cache = RepoCache(tmp_path / "cache")
    paths = await asyncio.gather(*(cache.ensure(str(source)) for _ in range(8)))
    assert len(set(paths)) == 1
    path = paths[0]
    await git("--git-dir", str(path), "fsck", "--full")
    (path / "HEAD").write_text("broken")
    assert await cache.ensure(str(source)) == path
    await git("--git-dir", str(path), "fsck", "--full")
    other = await source_repo(tmp_path / "other")
    other_path = await cache.ensure(str(other))
    await cache.ensure(str(source))
    await cache.prune(1)
    assert path.exists() and not other_path.exists()
    await cache.prune(0)
    assert not path.exists()


async def test_bad_remote_preserves_cache(tmp_path: Path):
    source = await source_repo(tmp_path / "source")
    cache = RepoCache(tmp_path / "cache")
    path = await cache.ensure(str(source))
    shutil.rmtree(source)
    with pytest.raises(InvalidTaskError):
        await cache.ensure(str(source))
    assert path.exists()


def test_tenant_pip_cache(tmp_path: Path):
    a = pip_cache_mount(tmp_path, "tenant-a")
    b = pip_cache_mount(tmp_path, "tenant-b")
    assert Path(a.source) == tmp_path / "pip/tenant-a"
    assert Path(a.source).is_dir() and a.source != b.source
    assert a.target == PIP_CACHE_ENV["PIP_CACHE_DIR"] == "/cache/pip"
    assert not a.read_only
    for tenant in ("", "..", "../escape", "/absolute", "a/b"):
        with pytest.raises(ValueError):
            pip_cache_mount(tmp_path, tenant)


async def test_cached_prepare_speed(tmp_path: Path):
    source = await source_repo(tmp_path / "source")
    # Incompressible loose objects make the first real transfer measurably expensive.
    for index in range(96):
        (source / f"blob-{index}").write_bytes(os.urandom(1024 * 1024))
    await git("-C", str(source), "add", ".")
    await git(
        "-C", str(source), "-c", "user.name=test", "-c", "user.email=t@t", "commit", "-m", "payload"
    )
    cache = RepoCache(tmp_path / "cache")
    timings = []
    for name in ("cold", "cached"):
        start = perf_counter()
        await GitWorkspace(tmp_path / name, f"sca/{name}", cache).prepare(source.as_uri(), "main")
        timings.append(perf_counter() - start)
    cold, cached = timings
    print(f"cold prepare: {cold:.3f}s; cached prepare: {cached:.3f}s; ratio: {cached / cold:.3f}")
    assert cached < cold / 2


async def test_different_urls_do_not_share_lock(tmp_path: Path):
    first = await source_repo(tmp_path / "first")
    second = await source_repo(tmp_path / "second")
    cache = RepoCache(tmp_path / "cache")
    async with cache.mirror(str(first)):
        waiting = asyncio.create_task(cache.ensure(str(first)))
        try:
            other = await asyncio.wait_for(cache.ensure(str(second)), timeout=5)
            assert other.exists() and not waiting.done()
        finally:
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
    assert await cache.ensure(str(first))
