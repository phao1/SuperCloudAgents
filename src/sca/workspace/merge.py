"""Fold trusted child mirrors using Git plumbing, without checking out repository content."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

from sca.core.errors import WorkspaceError


@dataclass
class SubtaskResult:
    name: str
    branch: str = ""
    commit: str = ""
    passed: bool = False
    changed: bool = False
    reason: str | None = None
    status: str = "failed"
    conflicts: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


MERGED_STATUSES = frozenset({"merged", "unchanged"})


async def merge_subtasks(
    mirror: Path,
    base: str,
    branch: str,
    children: list[tuple[Path, SubtaskResult]],
    task_id: str = "",
) -> tuple[str, list[SubtaskResult]]:
    async def git(*args: str, conflict_ok: bool = False) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "commit.gpgsign=false",
            "--git-dir",
            str(mirror),
            *args,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "SuperCloudAgents",
                "GIT_AUTHOR_EMAIL": "agents@supercloud.invalid",
                "GIT_COMMITTER_NAME": "SuperCloudAgents",
                "GIT_COMMITTER_EMAIL": "agents@supercloud.invalid",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode and not (conflict_ok and proc.returncode == 1):
            raise WorkspaceError(err.decode("utf-8", errors="replace"))
        return proc.returncode or 0, out.decode("utf-8", errors="replace")

    current = base
    await git("update-ref", f"refs/heads/{branch}", current)
    results = []
    by_name: dict[str, SubtaskResult] = {}
    for child_mirror, child in children:
        result = SubtaskResult(**vars(child))
        results.append(result)
        by_name[child.name] = result
        blocked = [
            name for name in child.depends_on
            if name not in by_name or by_name[name].status not in MERGED_STATUSES
        ]
        if blocked:
            result.status = "blocked"
            result.reason = result.reason or ("Dependencies did not merge: " + ", ".join(blocked))
            continue
        if not child.passed:
            result.status = "blocked" if child.status == "blocked" else "failed"
            continue
        if not child.changed:
            result.status = "unchanged"
            continue
        ref = f"refs/sca/subtasks/{child.name}"
        await git("fetch", "--no-tags", "--", str(child_mirror), f"+{child.branch}:{ref}")
        _, fetched = await git("rev-parse", ref)
        if fetched.strip() != child.commit:
            raise WorkspaceError(f"subtask {child.name} branch moved after checkpoint")
        code, output = await git(
            "merge-tree",
            "--write-tree",
            "--name-only",
            "-z",
            current,
            child.commit,
            conflict_ok=True,
        )
        if code:
            result.status = "conflict"
            result.conflicts = output.split("\0")[1 : output.split("\0").index("")]
            continue
        tree = output.split("\0", 1)[0].strip()
        _, commit = await git(
            "commit-tree",
            tree,
            "-p",
            current,
            "-p",
            child.commit,
            "-m",
            f"sca: merge {child.name}\n\nSca-Task-Id: {task_id}",
        )
        current = commit.strip()
        await git("update-ref", f"refs/heads/{branch}", current)
        result.status = "merged"
    return current, results
