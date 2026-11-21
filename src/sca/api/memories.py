from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from sca.api.authz import authorize
from sca.core.models import Principal
from sca.memory.embedding import embed
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
        body.model_dump(), user.subject, vector=await embed(body.text)
    )


@router.get("")
async def list_memories(
    request: Request, user: Reader, scope: str | None = None, status: Status | None = None,
    project_id: UUID | None = None,
) -> list[dict]:
    from sca.core.errors import InvalidTaskError

    try:
        return await request.app.state.store.for_tenant(user.tenant_id).list_memories(
            scope, status, project_id=project_id,
        )
    except ValueError as exc:
        raise InvalidTaskError(str(exc)) from exc


@router.patch("/{memory_id}")
async def patch(memory_id: UUID, body: MemoryPatch, request: Request, user: Writer) -> dict:
    from pydantic import ValidationError

    from sca.core.errors import InvalidTaskError

    try:
        store = request.app.state.store.for_tenant(user.tenant_id)
        changes = body.model_dump(exclude_none=True)
        rows = await store.list_memories()
        row = next((r for r in rows if r['id'] == memory_id), None)
        vector = None
        content = MemoryInput.model_validate({**row, **changes}).text if row else None
        if (row and changes.get('status', row['status']) == 'active'
                and (row['status'] != 'active' or changes.get('text', row['text']) != row['text'])):
            vector = await embed(content)
        return await store.change_memory(
            memory_id, user.subject, changes, vector=vector,
            vector_text=content)
    except (ValidationError, ValueError) as exc:
        raise InvalidTaskError(str(exc)) from exc


@router.delete("/{memory_id}", status_code=204)
async def delete(memory_id: UUID, request: Request, user: Writer) -> Response:
    await request.app.state.store.for_tenant(user.tenant_id).delete_memory(memory_id, user.subject)
    return Response(status_code=204)


@router.post('/reindex')
async def reindex(request: Request, user: Writer, scope: str | None = None) -> dict:
    store = request.app.state.store.for_tenant(user.tenant_id)
    count = 0
    for row in await store.list_memories(scope, 'active', missing_vectors=True):
        count += await store.reindex_memories(row, await embed(row['text']))
    return {'indexed': count}
