from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ValidationError

from sca.kinds import builtin as _builtin_kinds

_builtin_kinds.load()
from sca.api.authz import authorize
from sca.core.errors import InvalidTaskError, NotFoundError
from sca.core.models import Principal, Task
from sca.kernel.kind import get
from sca.observability import current_trace_id

router = APIRouter(prefix="/v1")


class Submission(BaseModel):
    kind: str
    spec: dict[str, Any]
    priority: Literal["interactive", "eval", "batch"] = "interactive"
    project_id: UUID | None = None


@router.post("/tasks", status_code=201)
async def submit(
    body: Submission,
    request: Request,
    user: Annotated[Principal, Depends(authorize("submit"))],
    idempotency_key: str | None = Header(None),
) -> Task:
    kind = get(body.kind)
    store = request.app.state.store.for_tenant(user.tenant_id)
    settings = getattr(request.app.state, "settings", None)

    def parse(values: dict) -> BaseModel:
        try:
            return kind.spec_model.model_validate(values)
        except ValidationError as exc:
            message = "; ".join(
                ".".join(map(str, error["loc"])) + ": " + error["msg"]
                for error in exc.errors(include_input=False)
            )
            raise InvalidTaskError(message) from exc

    def validate(values: dict) -> dict:
        spec = parse(values)
        if getattr(settings, "sandbox_driver", None) == "local":
            from sca.agents.local import host_agent_binary, unavailable_source_backend

            backend = spec.agent_backend or settings.agent_backend
            if backend != "fake" and host_agent_binary(settings, backend) is None:
                raise InvalidTaskError(unavailable_source_backend(backend))
        return {**spec.model_dump(), "priority": body.priority}

    if body.kind == "analysis":
        for file_id in parse(body.spec).inputs:
            try:
                await store.get_file(file_id)
            except NotFoundError as exc:
                raise InvalidTaskError("Input file not found in tenant") from exc
    task, created = await store.create_task(
        tenant_id=user.tenant_id,
        kind=body.kind,
        spec=body.spec,
        idempotency_key=idempotency_key,
        actor=user.subject,
        project_id=body.project_id,
        validate_spec=validate,
    )
    if created:
        request.app.state.metrics.created.inc()
        trace_id = current_trace_id()
        if trace_id:
            await store.update_result(task.id, {"trace_id": trace_id})
            task = await store.get_task(task.id)
    # Starting again repairs a previous submit whose database write succeeded.
    await request.app.state.orchestrator.start_task(str(task.id), task.kind)
    return task


