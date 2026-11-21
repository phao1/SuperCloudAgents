from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from sca.api.tools import Admin, Reader
from sca.config import get_settings
from sca.knowledge.models import (
    DocumentInput,
    DocumentMetadata,
    DocumentRead,
    SearchHit,
    SearchInput,
)
from sca.knowledge.service import ingest, read_document, search
from sca.tools.settings import get_tool_settings

router = APIRouter(prefix="/v1/knowledge")


class ProjectSearchInput(SearchInput):
    project_id: UUID | None = None


@router.post("/documents", status_code=201)
async def upload_document(body: DocumentInput, request: Request, user: Admin) -> DocumentMetadata:
    store = request.app.state.store.for_tenant(user.tenant_id)
    if body.project_id is not None:
        await store.get_project(body.project_id)
    return await ingest(store, body, user.subject, get_settings())


@router.get("/documents")
async def list_documents(request: Request, user: Reader,
                         limit: Annotated[int, Query(ge=1, le=100)] = 50,
                         offset: Annotated[int, Query(ge=0)] = 0,
                         project_id: UUID | None = None) -> list[DocumentMetadata]:
    store = request.app.state.store.for_tenant(user.tenant_id)
    if project_id is not None:
        await store.get_project(project_id)
    teams = get_tool_settings().teams_for(user.tenant_id, user.subject)
    return [DocumentMetadata.model_validate(row)
            for row in await store.list_knowledge_documents(teams, limit, offset, project_id=project_id)]


@router.get("/documents/{document_id}")
async def get_document(
    document_id: UUID, request: Request, user: Reader, project_id: UUID | None = None,
) -> DocumentRead:
    store = request.app.state.store.for_tenant(user.tenant_id)
    if project_id is not None:
        await store.get_project(project_id)
    teams = get_tool_settings().teams_for(user.tenant_id, user.subject)
    return await read_document(store, teams, document_id, project_id=project_id)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: UUID, request: Request, user: Admin) -> Response:
    await request.app.state.store.for_tenant(user.tenant_id).delete_knowledge_document(document_id)
    return Response(status_code=204)


@router.post("/search")
async def search_documents(body: ProjectSearchInput, request: Request, user: Reader) -> list[SearchHit]:
    store = request.app.state.store.for_tenant(user.tenant_id)
    if body.project_id is not None:
        await store.get_project(body.project_id)
    teams = get_tool_settings().teams_for(user.tenant_id, user.subject)
    return await search(store, teams, body.query, body.k, get_settings(), project_id=body.project_id)
