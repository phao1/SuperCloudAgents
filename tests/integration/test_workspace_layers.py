from pathlib import Path

import pytest
from test_workspace import git, source_repo

from sca.workspace.git import GitWorkspace
from sca.workspace.layers import LayerStore, disk_bytes

pytestmark = pytest.mark.integration


async def test_two_layered_workspaces_commit_diff_push(tmp_path: Path):
    source = await source_repo(tmp_path / "source")
    layers = LayerStore(tmp_path / "layers")
    first = GitWorkspace(tmp_path / "first", "sca/first", layers=layers)
    second = GitWorkspace(tmp_path / "second", "sca/second", layers=layers)
    prepared = await first.prepare(str(source), "main")
    await second.prepare(str(source), "main")
    assert len(list(layers.root.glob("*.refs"))) == 1
    assert await disk_bytes(second.workspace) < 1024 * 1024
    (second.workspace / "hello").write_text("changed\n")
    # This simulates the sandbox, rather than exercising host-side Git on used workspaces.
    await git("-C", str(second.workspace), "-c", "user.name=test", "-c", "user.email=t@t",
              "commit", "-am", "change")
    await second.fetch()
    assert "changed" in await second.diff(prepared.base_sha)
    await second.push(str(source))
    assert (first.workspace / "hello").read_text() != "changed\n"
