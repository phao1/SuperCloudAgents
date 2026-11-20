import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from sca.kinds import builtin as _builtin_kinds

_builtin_kinds.load()
from sca.api.authz import authorize
from sca.api.terminal import router as terminal_router
from sca.core.errors import ConflictError, InvalidTaskError
from sca.core.models import TERMINAL_STATES, Artifact, Event, Principal, Review, Task, TaskState
from sca.kernel.kind import all as all_kinds
from sca.kernel.kind import get
from sca.observability import current_trace_id
from sca.storage.local import key_for

router = APIRouter(prefix="/v1")
router.include_router(terminal_router)


class Submission(BaseModel):
    kind: str
    spec: dict[str, Any]


@router.get("/kinds")
async def list_kinds(user: Annotated[Principal, Depends(authorize("read"))]) -> list[dict]:
    return [
        {"name": kind.name, "schema": kind.spec_model.model_json_schema()} for kind in all_kinds()
    ]


@router.post("/tasks", status_code=201)
async def submit(
    body: Submission,
    request: Request,
    user: Annotated[Principal, Depends(authorize("submit"))],
    idempotency_key: str | None = Header(None),
) -> Task:
    kind = get(body.kind)
    try:
        spec = kind.spec_model.model_validate(body.spec)
    except ValidationError as exc:
        raise InvalidTaskError(str(exc)) from exc
    if body.kind == "analysis":
        from sca.core.errors import NotFoundError

        for file_id in spec.inputs:
            try:
                await request.app.state.store.for_tenant(user.tenant_id).get_file(file_id)
            except NotFoundError as exc:
                raise InvalidTaskError("Input file not found in tenant") from exc
    task, created = await request.app.state.store.for_tenant(user.tenant_id).create_task(
        tenant_id=user.tenant_id,
        kind=body.kind,
        spec=spec.model_dump(),
        idempotency_key=idempotency_key,
        actor=user.subject,
    )
    if created:
        request.app.state.metrics.created.inc()
        trace_id = current_trace_id()
        if trace_id:
            tenant_store = request.app.state.store.for_tenant(user.tenant_id)
            await tenant_store.update_result(task.id, {"trace_id": trace_id})
            task = await tenant_store.get_task(task.id)
    # Starting again repairs a previous submit whose database write succeeded.
    await request.app.state.orchestrator.start_task(str(task.id), task.kind)
    return task


@router.get("/tasks")
async def list_tasks(
    request: Request,
    user: Annotated[Principal, Depends(authorize("read"))],
    state: TaskState | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[Task]:
    return await request.app.state.store.for_tenant(user.tenant_id).list_tasks(
        user.tenant_id, state, limit
    )


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("read"))]
) -> Task:
    return await request.app.state.store.for_tenant(user.tenant_id).get_task(
        task_id, user.tenant_id
    )


@router.get("/tasks/{task_id}/events")
async def list_events(
    task_id: UUID,
    request: Request,
    user: Annotated[Principal, Depends(authorize("read"))],
    after: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=500),
) -> list[Event]:
    await request.app.state.store.for_tenant(user.tenant_id).get_task(task_id, user.tenant_id)
    return await request.app.state.store.for_tenant(user.tenant_id).list_events(
        task_id, after, limit
    )


@router.get("/tasks/{task_id}/events/stream")
async def stream(
    task_id: UUID,
    request: Request,
    user: Annotated[Principal, Depends(authorize("read"))],
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
    task_id: UUID, request: Request, user: Annotated[Principal, Depends(authorize("read"))]
) -> list[Artifact]:
    await request.app.state.store.for_tenant(user.tenant_id).get_task(task_id, user.tenant_id)
    return await request.app.state.store.for_tenant(user.tenant_id).list_artifacts(task_id)


@router.get("/tasks/{task_id}/artifacts/{name:path}")
async def download(
    task_id: UUID,
    name: str,
    request: Request,
    user: Annotated[Principal, Depends(authorize("read"))],
) -> Response:
    await request.app.state.store.for_tenant(user.tenant_id).get_task(task_id, user.tenant_id)
    artifact = await request.app.state.store.for_tenant(user.tenant_id).get_artifact(task_id, name)
    data = await request.app.state.object_store.get(key_for(task_id, artifact.name))
    return Response(data, media_type=artifact.content_type,
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
