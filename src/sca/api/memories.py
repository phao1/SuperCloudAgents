from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from sca.api.authz import authorize
from sca.core.models import Principal
from sca.memory.models import Category, MemoryInput, Status

router = APIRouter(prefix="/v1/memories")
Reader = Annotated[Principal, Depends(authorize("read"))]
Writer = Annotated[Principal, Depends(authorize("review"))]


class MemoryPatch(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=500)
    scope: str | None = Field(default=None, max_length=2000)
    category: Category | None = None
    status: Status | None = None


@router.post("", status_code=201)
async def create(body: MemoryInput, request: Request, user: Writer) -> dict:
    return await request.app.state.store.for_tenant(user.tenant_id).create_memory(
        body.model_dump(), user.subject
    )


@router.get("")
async def list_memories(
    request: Request, user: Reader, scope: str | None = None, status: Status | None = None
) -> list[dict]:
    from sca.core.errors import InvalidTaskError

    try:
        return await request.app.state.store.for_tenant(user.tenant_id).list_memories(scope, status)
    except ValueError as exc:
        raise InvalidTaskError(str(exc)) from exc


@router.patch("/{memory_id}")
async def patch(memory_id: UUID, body: MemoryPatch, request: Request, user: Writer) -> dict:
    from pydantic import ValidationError

    from sca.core.errors import InvalidTaskError

    try:
        return await request.app.state.store.for_tenant(user.tenant_id).change_memory(
            memory_id, user.subject, body.model_dump(exclude_none=True)
        )
    except (ValidationError, ValueError) as exc:
        raise InvalidTaskError(str(exc)) from exc


@router.delete("/{memory_id}", status_code=204)
async def delete(memory_id: UUID, request: Request, user: Writer) -> Response:
    await request.app.state.store.for_tenant(user.tenant_id).delete_memory(memory_id, user.subject)
    return Response(status_code=204)
