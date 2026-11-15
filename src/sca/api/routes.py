import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from sca.core.errors import ConflictError, InvalidTaskError
from sca.core.models import TERMINAL_STATES, Artifact, Event, Principal, Review, Task, TaskState
from sca.observability import current_trace_id
from sca.storage.local import key_for

router = APIRouter(prefix="/v1")


async def principal(request: Request, authorization: str | None = Header(None)) -> Principal:
    token = None
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            token = value
    return await request.app.state.authenticator.authenticate(token)


User = Annotated[Principal, Depends(principal)]


class Submission(BaseModel):
    kind: str
    spec: dict[str, Any]


@router.post("/tasks", status_code=201)
async def submit(
    body: Submission, request: Request, user: User, idempotency_key: str | None = Header(None)
) -> Task:
    if body.kind != "coding":
        raise InvalidTaskError("Unknown task kind")
    from sca.kinds.coding.spec import CodingSpec

    try:
        spec = CodingSpec.model_validate(body.spec)
    except ValidationError as exc:
        raise InvalidTaskError(str(exc)) from exc
    task, created = await request.app.state.store.for_tenant(user.tenant_id).create_task(
        tenant_id=user.tenant_id,
        kind=body.kind,
        spec=spec.model_dump(),
        idempotency_key=idempotency_key,
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
    user: User,
    state: TaskState | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[Task]:
    return await request.app.state.store.for_tenant(user.tenant_id).list_tasks(
        user.tenant_id, state, limit
    )


@router.get("/tasks/{task_id}")
async def get_task(task_id: UUID, request: Request, user: User) -> Task:
    return await request.app.state.store.for_tenant(user.tenant_id).get_task(
        task_id, user.tenant_id
    )


@router.get("/tasks/{task_id}/events")
async def list_events(
    task_id: UUID,
    request: Request,
    user: User,
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
    user: User,
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


@router.post("/tasks/{task_id}/review")
async def review(task_id: UUID, body: Review, request: Request, user: User) -> Task:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id, user.tenant_id)
    if task.state != TaskState.AWAITING_REVIEW:
        raise ConflictError("Task is not awaiting review")
    if body.decision == "request_changes" and not (body.feedback or "").strip():
        raise InvalidTaskError("Requesting changes requires feedback")
    await store.append_event(task_id, "review.submitted", body.model_dump())
    request.app.state.metrics.reviews.inc()
    await request.app.state.orchestrator.submit_review(str(task_id), body.decision, body.feedback)
    return await store.get_task(task_id, user.tenant_id)


class CiResult(BaseModel):
    commit: str = Field(min_length=7, max_length=64)
    passed: bool
    summary: str | None = Field(default=None, max_length=20000)
    url: str | None = Field(default=None, max_length=2000)


@router.post("/tasks/{task_id}/ci-result")
async def ci_result(task_id: UUID, body: CiResult, request: Request, user: User) -> Task:
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
async def cancel(task_id: UUID, request: Request, user: User) -> Task:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id, user.tenant_id)
    if task.state in TERMINAL_STATES:
        raise ConflictError("Task is already terminal")
    await request.app.state.orchestrator.cancel_task(str(task_id))
    return await store.get_task(task_id, user.tenant_id)


@router.get("/tasks/{task_id}/artifacts")
async def list_artifacts(task_id: UUID, request: Request, user: User) -> list[Artifact]:
    await request.app.state.store.for_tenant(user.tenant_id).get_task(task_id, user.tenant_id)
    return await request.app.state.store.for_tenant(user.tenant_id).list_artifacts(task_id)


@router.get("/tasks/{task_id}/artifacts/{name:path}")
async def download(task_id: UUID, name: str, request: Request, user: User) -> Response:
    await request.app.state.store.for_tenant(user.tenant_id).get_task(task_id, user.tenant_id)
    artifact = await request.app.state.store.for_tenant(user.tenant_id).get_artifact(task_id, name)
    data = await request.app.state.object_store.get(key_for(task_id, artifact.name))
    return Response(data, media_type=artifact.content_type)


@router.post("/tasks/{task_id}/pause")
async def pause(task_id: UUID, request: Request, user: User) -> Task:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id)
    if task.state in TERMINAL_STATES or task.state == TaskState.PAUSED:
        raise ConflictError("Task is terminal or already paused")
    await request.app.state.orchestrator.pause_task(str(task_id))
    return await store.get_task(task_id)


@router.post("/tasks/{task_id}/resume")
async def resume(task_id: UUID, request: Request, user: User) -> Task:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id)
    if task.state != TaskState.PAUSED:
        raise ConflictError("Task is not paused")
    await request.app.state.orchestrator.resume_task(str(task_id))
    return await store.get_task(task_id)
