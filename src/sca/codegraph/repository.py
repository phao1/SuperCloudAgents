"""Index and diff immutable commits through Git plumbing in trusted bare mirrors."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import TypeGuard

from pydantic import ValidationError

from sca.codegraph.index import MAX_SOURCE_BYTES, CodeIndex, Impact, analyze_impact, build_index
from sca.codegraph.parsers import DEFAULT_PARSERS
from sca.core.errors import WorkspaceError

_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")


def is_oid(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _OID.fullmatch(value) is not None


def display_path(path: bytes) -> str:
    return path.decode("utf-8", errors="backslashreplace")


def _argv(mirror: Path, *args: str) -> list[str]:
    return [
        "git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "--git-dir", str(mirror), *args,
    ]


def _env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _git(mirror: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            _argv(mirror, *args), env=_env(), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=True, timeout=120,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceError(f"无法读取 Git 数据（{args[0]}）") from exc


async def changed_paths(mirror: Path, before: str, after: str) -> list[bytes]:
    if not is_oid(before) or not is_oid(after):
        raise WorkspaceError("无效的 Git 提交")
    output = await asyncio.to_thread(
        _git, mirror, "diff", "--name-only", "-z", "--no-renames", "--no-ext-diff",
        "--no-textconv", "--ignore-submodules=none", before, after, "--",
    )
    return [path for path in output.split(b"\0") if path]


def _sources(mirror: Path, commit: str) -> Iterator[tuple[str, bytes | None]]:
    tree = _git(mirror, "ls-tree", "-r", "-l", "-z", "--full-tree", commit)
    argv = _argv(mirror, "cat-file", "--batch")
    with subprocess.Popen(
        argv, env=_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as batch:
        assert batch.stdin is not None and batch.stdout is not None
        try:
            for entry in tree.split(b"\0"):
                if not entry:
                    continue
                meta, raw_path = entry.split(b"\t", 1)
                mode, _, oid, size = meta.split()
                path = display_path(raw_path)
                if (mode not in {b"100644", b"100755"} or
                        PurePosixPath(path).suffix not in DEFAULT_PARSERS or
                        int(size) > MAX_SOURCE_BYTES):
                    yield path, None
                    continue
                batch.stdin.write(oid + b"\n")
                batch.stdin.flush()
                header = batch.stdout.readline().split()
                if header != [oid, b"blob", size]:
                    raise WorkspaceError("无法读取 Git 文件内容")
                source = batch.stdout.read(int(size))
                if len(source) != int(size) or batch.stdout.read(1) != b"\n":
                    raise WorkspaceError("Git 文件内容不完整")
                yield path, source
        finally:
            batch.stdin.close()


def _cached_index(mirror: Path, commit: str, cache: Path) -> CodeIndex:
    try:
        return CodeIndex.model_validate_json(cache.read_bytes())
    except (FileNotFoundError, ValidationError):
        pass
    with closing(_sources(mirror, commit)) as sources:
        index = build_index(sources)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=cache.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(index.model_dump_json().encode())
            file.close()
            temporary.replace(cache)
        finally:
            temporary.unlink(missing_ok=True)
    return index


async def index_commit(mirror: Path, commit: str, layers: Path, tenant_id: str) -> CodeIndex:
    if not is_oid(commit):
        raise WorkspaceError("无效的 Git 提交")
    tenant = hashlib.sha256(tenant_id.encode()).hexdigest()
    cache = layers / "codegraph-v1" / tenant / f"{commit}.json"
    return await asyncio.to_thread(_cached_index, mirror, commit, cache)


async def analyze_patch(
    mirror: Path, base_sha: str, commit: str, layers: Path, tenant_id: str,
) -> Impact:
    paths = await changed_paths(mirror, base_sha, commit)
    if not paths:
        return Impact(changed_paths=[], impacted_files=[], tests_to_run=[],
                      base_sha=base_sha, commit=commit)
    before, after = await asyncio.gather(
        index_commit(mirror, base_sha, layers, tenant_id),
        index_commit(mirror, commit, layers, tenant_id),
    )
    result = analyze_impact(before, after, changed_paths=map(display_path, paths))
    return result.model_copy(update={"base_sha": base_sha, "commit": commit})
