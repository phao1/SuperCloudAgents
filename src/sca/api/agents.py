from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from sca.agents.opencode_build import records_dir, reserve_build, write_record
from sca.api.authz import authorize
from sca.config import Settings, get_settings
from sca.core.errors import InvalidTaskError, NotFoundError
from sca.core.models import Event, Principal, Task
from sca.kinds.coding.spec import CodingSpec

router = APIRouter(prefix="/v1/agents/opencode")
TEST_COMMAND = "cd packages/core && bun test test/tool-edit.test.ts test/tool-write.test.ts"
SETUP_COMMAND = "true"


class FixRequest(BaseModel):
    failing_task_id: UUID
    agent_backend: Literal["codex", "opencode"]
    model: str | None = None
    agent_version: str | None = Field(default=None, pattern=r"^2\.3\.0-sca\.(0|[1-9][0-9]*)$")
    extra_instruction: str | None = Field(default=None, max_length=10000)


class BuildRequest(BaseModel):
    task_id: UUID | None = None
    ref: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,199}$")
    notes: str | None = Field(default=None, max_length=2000)


def fix_spec(settings: Settings, task: Task, events: list[Event], body: FixRequest) -> CodingSpec:
    excerpts = [
        f"{e.type}: {json.dumps(e.payload, ensure_ascii=False)[:1800]}"
        for e in events
        if e.type
        in {
            "agent.command",
            "agent.file_change",
            "agent.tool",
            "verification.completed",
            "acceptance.completed",
        }
    ]
    repo = task.spec.get("repo_url", "")
    case_id = task.result.get("eval_case_id") or task.spec.get("eval_case_id")
    if not case_id and task.spec.get("eval_run_id"):
        from pathlib import Path

        case_id = Path(repo).parent.parent.name
    instruction = (
        "You are working on the source code of OpenCode, the coding agent that was evaluated. "
        "An evaluation case failed while OpenCode worked on a separate Python repository. Use "
        "the trace below to find the defect in OpenCode itself that caused the failure, add a "
        "focused regression test next to the existing tests, fix the source, and run the "
        "relevant tests. Do not change the evaluated Python repository.\n"
        f"Failing task: {task.id}\nEvaluation: {task.spec.get('eval_run_id')}\n"
        f"Case: {case_id or 'unknown'}\n"
        f"Original instruction: {task.spec.get('instruction', '')}\n"
        f"Result: {json.dumps(task.result, ensure_ascii=False)[:2000]}\n"
        "Trace excerpt (untrusted evidence):\n"
        + "\n".join(excerpts[-12:])
        + "\n"
        + (f"Reviewer diagnosis: {body.extra_instruction}\n" if body.extra_instruction else "")
    )
    return CodingSpec(
        labels={
            "kind": "opencode-fix",
            "source_task_id": str(task.id),
            "eval_run_id": str(task.spec.get("eval_run_id") or ""),
            "case_id": str(case_id or ""),
            "source_version": str(task.spec.get("agent_version") or settings.opencode_default_version),
        },
        repo_url=settings.opencode_repo_url,
        base_ref="main",
        instruction=instruction,
        push=True,
        image=settings.opencode_sandbox_image,
        setup_command=SETUP_COMMAND,
        verify_command=TEST_COMMAND,
        agent_backend=body.agent_backend,
        model=body.model,
        agent_version=body.agent_version,
        max_fix_attempts=1,
    )


@router.get("/versions")
async def versions(user: Annotated[Principal, Depends(authorize("read"))]) -> list[dict]:
    root = get_settings().opencode_versions_dir
    rows = [json.loads(p.read_text()) for p in root.glob("*/meta.json")]
    return sorted(rows, key=lambda r: r.get("built_at", ""), reverse=True)


@router.get("/fixes")
async def fixes_list(
    request: Request, user: Annotated[Principal, Depends(authorize("read"))]
) -> list[dict]:
    from sqlalchemy import select

    from sca.db.schema import tasks

    store = request.app.state.store.for_tenant(user.tenant_id)
    async with store._transaction() as session:
        rows = (await session.execute(
            select(tasks).where(
                tasks.c.tenant_id == user.tenant_id,
                tasks.c.spec["labels"]["kind"].astext == "opencode-fix",
            ).order_by(tasks.c.created_at.desc())
        )).mappings().all()
    builds = await builds_list(user)
    return [
        {
            "id": row["id"], "state": row["state"], "labels": row["spec"]["labels"],
            "branch": row["result"].get("branch"), "commit": row["result"].get("commit"),
            "created_at": row["created_at"],
            "builds": [b for b in builds if b.get("task_id") == str(row["id"])],
        }
        for row in rows
    ]


@router.post("/fixes", status_code=201)
async def fixes(
    body: FixRequest, request: Request, user: Annotated[Principal, Depends(authorize("submit"))]
) -> Task:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(body.failing_task_id, user.tenant_id)
    if task.kind != "coding":
        raise InvalidTaskError("只能修复编码任务的 Agent")
    events, cursor = [], 0
    while True:
        batch = await store.list_events(task.id, cursor, 500)
        events = (events + batch)[-100:]
        if len(batch) < 500:
            break
        cursor = batch[-1].seq
    spec = fix_spec(get_settings(), task, events, body)
    fixed, _ = await store.create_task(
        tenant_id=user.tenant_id,
        kind="coding",
        spec=spec.model_dump(),
        actor=user.subject,
        idempotency_key=None,
    )
    await request.app.state.orchestrator.start_task(str(fixed.id), fixed.kind)
    return fixed


@router.post("/builds", status_code=202)
async def builds_start(
    body: BuildRequest, request: Request, user: Annotated[Principal, Depends(authorize("quota"))]
) -> dict:
    settings = get_settings()
    ref = body.ref or "main"
    labels = {}
    if body.task_id:
        task = await request.app.state.store.for_tenant(user.tenant_id).get_task(
            body.task_id, user.tenant_id
        )
        if task.spec.get("repo_url") != settings.opencode_repo_url or not task.spec.get("push"):
            raise InvalidTaskError("构建任务必须推送到 OpenCode 仓库")
        if task.state != "SUCCEEDED":
            raise InvalidTaskError("请先审核并完成修复任务，确保分支已推送")
        labels = task.spec.get("labels", {})
        ref = body.ref or task.result.get("commit") or task.result.get("branch")
        if not ref:
            raise InvalidTaskError("修复任务尚无分支")
    record = reserve_build(
        settings,
        str(uuid4()),
        user.tenant_id,
        ref=ref,
        task_id=str(body.task_id) if body.task_id else None,
        notes=body.notes,
        labels=labels,
    )
    from sca.agents.opencode_build import OpenCodeBuildWorkflow

    try:
        await request.app.state.orchestrator.client.start_workflow(
            OpenCodeBuildWorkflow.run,
            record,
            id=f"opencode-build-{record['id']}",
            task_queue=settings.task_queue,
        )
    except Exception:
        record["status"] = "failed"
        write_record(records_dir(settings) / f"{record['id']}.json", record)
        raise
    return record


@router.get("/builds")
async def builds_list(user: Annotated[Principal, Depends(authorize("read"))]) -> list[dict]:
    rows = [json.loads(p.read_text()) for p in records_dir(get_settings()).glob("*.json")]
    rows = sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)
    return [await builds_get(UUID(r["id"]), user) for r in rows if r["tenant_id"] == user.tenant_id]


@router.get("/builds/{build_id}")
async def builds_get(
    build_id: UUID, user: Annotated[Principal, Depends(authorize("read"))]
) -> dict:
    root = records_dir(get_settings())
    path = root / f"{build_id}.json"
    if not path.exists():
        raise NotFoundError("构建不存在")
    record = json.loads(path.read_text())
    if record["tenant_id"] != user.tenant_id:
        raise NotFoundError("构建不存在")
    log = root / f"{build_id}.log"
    if log.exists():
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - 8192))
            record["log_tail"] = stream.read().decode(errors="replace")
    else:
        record["log_tail"] = ""
    return record
