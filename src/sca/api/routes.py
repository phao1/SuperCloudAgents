import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from sca.kinds import builtin as _builtin_kinds

_builtin_kinds.load()
from sca.api.authz import ForbiddenError, GuestPrincipal, authorize
from sca.api.guest_history import (
    guest_artifact_text,
    guest_downloadable,
    history_artifacts,
    history_event,
    history_message,
    history_payload,
    history_task,
)
from sca.api.tasks_admission import submit
from sca.api.terminal import router as terminal_router
from sca.core.errors import ConflictError, InvalidTaskError, NotFoundError
from sca.core.models import (
    TERMINAL_STATES,
    Artifact,
    Event,
    Principal,
    Review,
    Task,
    TaskMessage,
    TaskMessageInput,
    TaskState,
)
from sca.kernel.kind import all as all_kinds
from sca.kinds.coding.best_of import CHOSEN_KEY, PLAN_KEY, SELECT_DECISION_PREFIX, candidate_key
from sca.storage.local import history_artifact_key, key_for

router = APIRouter(prefix="/v1")
router.include_router(terminal_router)
router.post("/tasks", status_code=201)(submit)


@router.get("/kinds")
async def list_kinds(user: Annotated[Principal, Depends(authorize("read"))]) -> list[dict]:
    return [
        {"name": kind.name, "schema": kind.spec_model.model_json_schema()} for kind in all_kinds()
    ]


@router.get("/tasks")
async def list_tasks(
    request: Request,
    user: Annotated[Principal, Depends(authorize("read", guest=True))],
    state: TaskState | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[Task]:
    store = request.app.state.store.for_tenant(user.tenant_id)
    return [history_task(user, task) for task in await store.list_tasks(user.tenant_id, state, limit)]


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("read", guest=True))]
) -> Task:
    store = request.app.state.store.for_tenant(user.tenant_id)
    return history_task(user, await store.get_task(task_id, user.tenant_id))


@router.get("/tasks/{task_id}/events")
async def list_events(
    task_id: UUID,
    request: Request,
    user: Annotated[Principal, Depends(authorize("read", guest=True))],
    after: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=500),
) -> list[Event]:
    store = request.app.state.store.for_tenant(user.tenant_id)
    await store.get_task(task_id, user.tenant_id)
    return [history_event(user, event) for event in await store.list_events(task_id, after, limit)]


@router.get("/tasks/{task_id}/events/stream")
async def stream(
    task_id: UUID,
    request: Request,
    user: Annotated[Principal, Depends(authorize("read", guest=True))],
    after: int = Query(0, ge=0),
    last_event_id: str | None = Header(None),
) -> StreamingResponse:
    store = request.app.state.store.for_tenant(user.tenant_id)
    await store.get_task(task_id, user.tenant_id)
    if last_event_id is not None:
        try:
            after = int(last_event_id)
            if after < 0:
                raise ValueError
        except ValueError:
            raise InvalidTaskError("Last-Event-ID must be a nonnegative integer") from None

    async def generate() -> AsyncIterator[str]:
        listener = request.app.state.event_listener
        wakeup = listener.subscribe(str(task_id))
        try:
            cursor, keepalive = after, time.monotonic()
            while not await request.is_disconnected():
                wakeup.clear()
                # Read state first so a terminal state cannot hide its final events.
                task = await store.get_task(task_id, user.tenant_id)
                batch = await store.list_events(task_id, cursor)
                for event in batch:
                    event = history_event(user, event)
                    yield f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
                    cursor = event.seq
                if task.state in TERMINAL_STATES and len(batch) < 500:
                    return
                if len(batch) == 500:
                    continue
                if time.monotonic() - keepalive >= 15:
                    yield ": keep-alive\n\n"
                    keepalive = time.monotonic()
                try:
                    await asyncio.wait_for(wakeup.wait(), 5)
                except TimeoutError:
                    pass
        finally:
            listener.unsubscribe(str(task_id), wakeup)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class MemoryReview(Review):
    accept_memories: list[UUID] = Field(default_factory=list, max_length=30)


@router.post("/tasks/{task_id}/review")
async def review(
    task_id: UUID,
    body: MemoryReview,
    request: Request,
    user: Annotated[Principal, Depends(authorize("review"))],
) -> Task:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id, user.tenant_id)
    if task.state != TaskState.AWAITING_REVIEW:
        raise ConflictError("Task is not awaiting review")
    if _awaiting_best_of_selection(task):
        raise ConflictError("请先选择 Best-of-N 候选结果")
    if body.decision == "request_changes" and not (body.feedback or "").strip():
        raise InvalidTaskError("Requesting changes requires feedback")
    if body.accept_memories and body.decision != "approve":
        raise InvalidTaskError("Memories can only be accepted with task approval")
    proposals = set(task.result.get("memory_proposals", []))
    if any(str(mid) not in proposals for mid in body.accept_memories):
        raise InvalidTaskError("Memory does not belong to this task's proposals")
    vectors = {}
    if body.accept_memories:
        from sca.memory.embedding import embed

        # Embed before the store opens its transaction: a gateway call must not hold row locks.
        wanted = set(body.accept_memories)
        for row in await store.list_memories(None, "proposed"):
            if row["id"] in wanted:
                vectors[row["id"]] = await embed(row["text"])
    await store.accept_memories(task_id, body.accept_memories, user.subject, vectors=vectors)
    await store.append_event(
        task_id, "review.submitted", {**body.model_dump(mode="json"), "actor": user.subject}
    )
    request.app.state.metrics.reviews.inc()
    await request.app.state.orchestrator.submit_review(str(task_id), body.decision, body.feedback)
    return await store.get_task(task_id, user.tenant_id)


class CiResult(BaseModel):
    commit: str = Field(min_length=7, max_length=64)
    passed: bool
    summary: str | None = Field(default=None, max_length=20000)
    url: str | None = Field(default=None, max_length=2000)


@router.post("/tasks/{task_id}/ci-result")
async def ci_result(
    task_id: UUID,
    body: CiResult,
    request: Request,
    user: Annotated[Principal, Depends(authorize("ci-result"))],
) -> Task:
    """Called by the project's CI pipeline when it has finished with the task's branch."""
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id, user.tenant_id)
    if task.state in TERMINAL_STATES:
        raise ConflictError("Task is already terminal")
    await request.app.state.orchestrator.submit_ci_result(
        str(task_id), body.commit, body.passed, body.summary, body.url
    )
    return await store.get_task(task_id, user.tenant_id)


@router.post("/tasks/{task_id}/cancel")
async def cancel(
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("cancel"))]
) -> Task:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id, user.tenant_id)
    if task.state in TERMINAL_STATES:
        raise ConflictError("Task is already terminal")
    await store.append_event(task_id, "task.cancel_requested", {"actor": user.subject})
    await request.app.state.orchestrator.cancel_task(str(task_id))
    return await store.get_task(task_id, user.tenant_id)


@router.get("/tasks/{task_id}/artifacts")
async def list_artifacts(
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("read", guest=True))]
) -> list[Artifact]:
    store = request.app.state.store.for_tenant(user.tenant_id)
    await store.get_task(task_id, user.tenant_id)
    return history_artifacts(user, await store.list_artifacts(task_id))


@router.get("/tasks/{task_id}/artifacts/{name:path}")
async def download(
    task_id: UUID,
    name: str,
    request: Request,
    user: Annotated[Principal, Depends(authorize("read", guest=True))],
) -> Response:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id, user.tenant_id)
    artifact = await store.get_artifact(task_id, name)
    guest = isinstance(user, GuestPrincipal)
    if guest and not guest_downloadable(artifact):
        raise ForbiddenError("访客只能读取文本制品")
    if reason := artifact.missing_reason:
        return JSONResponse({"error": {"code": "artifact_missing", "message": reason}}, status_code=410)
    artifact_key = history_artifact_key if task.result.get("history_imported") is True else key_for
    data = await request.app.state.object_store.get(artifact_key(task_id, artifact.name))
    return Response(guest_artifact_text(data) if guest else data,
                    media_type="text/plain" if guest else artifact.content_type,
                    headers={"Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"})


@router.post("/tasks/{task_id}/pause")
async def pause(
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("pause"))]
) -> Task:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id)
    if task.state in TERMINAL_STATES or task.state == TaskState.PAUSED:
        raise ConflictError("Task is terminal or already paused")
    await store.append_event(task_id, "task.pause_requested", {"actor": user.subject})
    await request.app.state.orchestrator.pause_task(str(task_id))
    return await store.get_task(task_id)


@router.post("/tasks/{task_id}/resume")
async def resume(
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("resume"))]
) -> Task:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id)
    if task.state != TaskState.PAUSED:
        raise ConflictError("Task is not paused")
    if task.project_id is not None and await store.project_work_blocked(str(task_id)):
        raise ConflictError("请先恢复此项目")
    await store.append_event(task_id, "task.resume_requested", {"actor": user.subject})
    await request.app.state.orchestrator.resume_task(str(task_id))
    return await store.get_task(task_id)


class TokenRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: Literal["viewer", "member", "admin"] | None = None
    scopes: list[Literal["ci:report"]] = Field(default_factory=list)
    repo_url: str | None = Field(default=None, max_length=2000)
    days: int = Field(default=90, ge=1, le=3650)


@router.post("/tokens", status_code=201)
async def create_token(
    body: TokenRequest,
    request: Request,
    user: Annotated[Principal, Depends(authorize("tokens"))],
) -> dict[str, Any]:
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta

    token = "sca_" + secrets.token_urlsafe(32)
    row = await request.app.state.store.for_tenant(user.tenant_id).create_api_token(
        name=body.name,
        role=body.role,
        scopes=body.scopes,
        repo_url=body.repo_url,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(days=body.days),
        created_by=user.subject,
    )
    return {**row, "token": token}


@router.get("/tokens")
async def list_tokens(
    request: Request,
    user: Annotated[Principal, Depends(authorize("tokens"))],
) -> list[dict[str, Any]]:
    return await request.app.state.store.for_tenant(user.tenant_id).list_api_tokens()


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_token(
    token_id: UUID,
    request: Request,
    user: Annotated[Principal, Depends(authorize("tokens"))],
) -> Response:
    await request.app.state.store.for_tenant(user.tenant_id).revoke_api_token(token_id)
    return Response(status_code=204)


# Best-of-N candidate review.


class BestOfSelection(BaseModel):
    attempt: int = Field(ge=1, le=5, strict=True)


def _awaiting_best_of_selection(task: Task) -> bool:
    return (
        task.state == TaskState.AWAITING_REVIEW
        and (task.spec.get("best_of") or {}).get("select") == "human"
        and task.result.get(CHOSEN_KEY) is None
    )


@router.get("/tasks/{task_id}/best-of")
async def get_best_of(
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("read", guest=True))],
) -> dict:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id, user.tenant_id)
    options = task.spec.get("best_of")
    if task.kind != "coding" or not options:
        raise NotFoundError("此任务未启用 Best-of-N")
    chosen = task.result.get(CHOSEN_KEY)
    attempts = []
    for plan in task.result.get(PLAN_KEY, []):
        attempt = plan["attempt"]
        candidate = task.result.get(candidate_key(attempt))
        artifact = candidate.get("patch_artifact") if candidate else None
        data = {
            **plan, "verification": None, "cost_usd": None, "turns": 0, "diff_stats": None,
            **(candidate or {}), "chosen": attempt == chosen,
            "completed": candidate is not None,
            "patch_url": f"/v1/tasks/{task_id}/artifacts/{artifact}" if artifact else None,
        }
        if data["diff_stats"] is not None:
            stats = data["diff_stats"]
            data["diff_stats"] = {**stats, "changed_lines": stats["insertions"] + stats["deletions"]}
        attempts.append(data)
    return history_payload(user, {
        "select": options.get("select", "verify_then_cost"), "chosen_attempt": chosen,
        "awaiting_selection": _awaiting_best_of_selection(task), "attempts": attempts,
    })


@router.post("/tasks/{task_id}/best-of/select", status_code=202)
async def select_best_of(
    task_id: UUID,
    body: BestOfSelection,
    request: Request,
    user: Annotated[Principal, Depends(authorize("review"))],
) -> BestOfSelection:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id, user.tenant_id)
    options = task.spec.get("best_of")
    if task.kind != "coding" or not options:
        raise NotFoundError("此任务未启用 Best-of-N")
    if options.get("select") != "human":
        raise ConflictError("此任务使用自动选择")
    if task.result.get(CHOSEN_KEY) == body.attempt:
        return body
    if not _awaiting_best_of_selection(task):
        raise ConflictError("此任务当前未等待选择候选结果")
    candidate = task.result.get(candidate_key(body.attempt))
    if candidate is None or candidate.get("patch_artifact") is None:
        raise InvalidTaskError("该尝试没有可选择的补丁")
    await request.app.state.orchestrator.submit_review(
        str(task_id), f"{SELECT_DECISION_PREFIX}{body.attempt}", user.subject,
    )
    return body


# Task interjection
@router.post("/tasks/{task_id}/messages", status_code=201)
async def send_task_message(
    task_id: UUID,
    body: TaskMessageInput,
    request: Request,
    user: Annotated[Principal, Depends(authorize("task-message"))],
) -> TaskMessage:
    store = request.app.state.store.for_tenant(user.tenant_id)
    message = await store.add_task_message(task_id, body, user.subject)
    await request.app.state.orchestrator.submit_task_message(str(task_id), str(message.id), body.mode)
    return message


@router.get("/tasks/{task_id}/messages")
async def list_task_messages(
    task_id: UUID,
    request: Request,
    user: Annotated[Principal, Depends(authorize("read", guest=True))],
) -> list[TaskMessage]:
    store = request.app.state.store.for_tenant(user.tenant_id)
    return [history_message(user, message) for message in await store.list_task_messages(task_id)]
