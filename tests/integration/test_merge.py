"""Merge plumbing works on bare mirrors without worktrees."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sca.workspace.merge import SubtaskResult, merge_subtasks


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.mark.parametrize("mode", ["files", "hunks", "conflict", "unchanged"])
async def test_merge(tmp_path, mode):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "a").write_text("first\n" + "middle\n" * 20 + "last\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    children = []
    for name in ["one", "two"]:
        git(repo, "checkout", "-b", name, base)
        if mode == "files":
            (repo / name).write_text(name)
        elif mode != "unchanged":
            content = (repo / "a").read_text()
            target = "last" if mode == "hunks" and name == "two" else "first"
            (repo / "a").write_text(content.replace(target, name))
        git(repo, "add", ".")
        git(repo, "commit", "--allow-empty", "-m", name)
        commit = git(repo, "rev-parse", "HEAD")
        mirror = tmp_path / f"{name}.git"
        git(tmp_path, "clone", "--bare", str(repo), str(mirror))
        children.append((mirror, SubtaskResult(name, name, commit, True, mode != "unchanged")))
    parent = tmp_path / "parent.git"
    git(tmp_path, "clone", "--bare", str(repo), str(parent))
    commit, results = await merge_subtasks(parent, base, "sca/task", children)
    assert git(parent, "rev-parse", "sca/task") == commit
    if mode == "conflict":
        assert [r.status for r in results] == ["merged", "conflict"]
        assert results[1].conflicts == ["a"]
        assert git(parent, "show", f"{commit}:a").startswith("one")
    elif mode == "unchanged":
        assert commit == base and all(r.status == "unchanged" for r in results)
    else:
        assert all(r.status == "merged" for r in results)
        if mode == "files":
            assert git(parent, "show", f"{commit}:one") == "one"
            assert git(parent, "show", f"{commit}:two") == "two"
        else:
            content = git(parent, "show", f"{commit}:a")
            assert content.startswith("one") and content.endswith("two")
