from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ValidationError

from sca.kinds import builtin as _builtin_kinds

_builtin_kinds.load()
from sca.api.authz import authorize
from sca.api.model_access import require_model
from sca.core.errors import InvalidTaskError
from sca.core.models import Principal, Task
from sca.kernel.kind import get
from sca.observability import current_trace_id

router = APIRouter(prefix="/v1")


class Submission(BaseModel):
    kind: str
    spec: dict[str, Any]
    priority: Literal["interactive", "eval", "batch"] = "interactive"


@router.post("/tasks", status_code=201)
async def submit(
    body: Submission,
    request: Request,
    user: Annotated[Principal, Depends(authorize("submit"))],
    idempotency_key: str | None = Header(None),
) -> Task:
    require_model()
    kind = get(body.kind)
    try:
        spec = kind.spec_model.model_validate(body.spec)
    except ValidationError as exc:
        raise InvalidTaskError(str(exc)) from exc
    settings = getattr(request.app.state, "settings", None)
    if getattr(settings, "sandbox_driver", None) == "local":
        from sca.agents.local import host_agent_binary, unavailable_source_backend

        backend = spec.agent_backend or settings.agent_backend
        if backend != "fake" and host_agent_binary(settings, backend) is None:
            raise InvalidTaskError(unavailable_source_backend(backend))
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
        spec={**spec.model_dump(), "priority": body.priority},
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


