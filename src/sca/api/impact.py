"""Read the impact of a coding task's published patch."""

from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from sca.api.authz import authorize
from sca.codegraph.index import Impact
from sca.codegraph.repository import analyze_patch, is_oid
from sca.core.errors import ConflictError, NotFoundError, WorkspaceError
from sca.core.interfaces import ObjectStore
from sca.core.models import EventType, Principal
from sca.workspace.git import GitWorkspace
from sca.workspace.state import TaskStateStore

router = APIRouter(prefix="/v1")


@router.get("/tasks/{task_id}/impact")
async def task_impact(
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("read"))],
) -> Impact:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id, user.tenant_id)
    artifact = task.result.get("patch_artifact")
    if task.kind != "coding" or not artifact:
        raise NotFoundError("此任务尚未发布代码补丁")

    # A checkpoint in a later review round can be newer than the published patch.
    events = await store.list_events(task_id, limit=1_000_000)
    commit = next(
        (event.payload.get("commit") for event in reversed(events)
         if event.type == EventType.ARTIFACT_PUBLISHED and event.payload.get("name") == artifact),
        None,
    )
    base = task.result.get("base_sha")
    if not is_oid(base) or not is_oid(commit):
        raise ConflictError("任务补丁对应的 Git 提交暂不可用")

    settings = request.app.state.settings
    layers = settings.data_dir / "layers"
    mirror = settings.task_dir(str(task_id)) / "mirror.git"
    if mirror.is_dir():
        try:
            return await analyze_patch(mirror, base, commit, layers, task.tenant_id)
        except WorkspaceError:
            pass
    try:
        return await _from_bundle(
            request.app.state.object_store, str(task_id), base, commit, layers, task.tenant_id,
        )
    except (NotFoundError, WorkspaceError):
        raise ConflictError("任务补丁对应的仓库版本暂不可用") from None


async def _from_bundle(
    objects: ObjectStore, task_id: str, base: str, commit: str, layers: Path, tenant_id: str,
) -> Impact:
    state = TaskStateStore(objects, task_id)
    manifest = await state.manifest()
    digest = (manifest or {}).get("sha256", {}).get("mirror.bundle")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise WorkspaceError("任务的 Git 快照缺失")
    data = await objects.get(f"{state.prefix}/blobs/{digest}")
    if hashlib.sha256(data).hexdigest() != digest:
        raise WorkspaceError("任务的 Git 快照校验失败")
    with tempfile.TemporaryDirectory(prefix="sca-impact-") as directory:
        ws = GitWorkspace(Path(directory), "sca/impact")
        bundle = ws.task_dir / "mirror.bundle"
        await asyncio.to_thread(bundle.write_bytes, data)
        await ws._git("clone", "--mirror", "--", str(bundle), str(ws.mirror))
        return await analyze_patch(ws.mirror, base, commit, layers, tenant_id)
