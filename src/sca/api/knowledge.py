from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from sca.api.tools import Admin, Reader
from sca.config import get_settings
from sca.knowledge.models import DocumentInput, DocumentMetadata, SearchHit, SearchInput
from sca.knowledge.service import ingest, search
from sca.tools.settings import get_tool_settings

router = APIRouter(prefix="/v1/knowledge")


@router.post("/documents", status_code=201)
async def upload_document(body: DocumentInput, request: Request, user: Admin) -> DocumentMetadata:
    store = request.app.state.store.for_tenant(user.tenant_id)
    return await ingest(store, body, user.subject, get_settings())


@router.get("/documents")
async def list_documents(request: Request, user: Reader,
                         limit: Annotated[int, Query(ge=1, le=100)] = 50,
                         offset: Annotated[int, Query(ge=0)] = 0) -> list[DocumentMetadata]:
    store = request.app.state.store.for_tenant(user.tenant_id)
    teams = get_tool_settings().teams_for(user.tenant_id, user.subject)
    return [DocumentMetadata.model_validate(row)
            for row in await store.list_knowledge_documents(teams, limit, offset)]


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: UUID, request: Request, user: Admin) -> Response:
    await request.app.state.store.for_tenant(user.tenant_id).delete_knowledge_document(document_id)
    return Response(status_code=204)


@router.post("/search")
async def search_documents(body: SearchInput, request: Request, user: Reader) -> list[SearchHit]:
    store = request.app.state.store.for_tenant(user.tenant_id)
    teams = get_tool_settings().teams_for(user.tenant_id, user.subject)
    return await search(store, teams, body.query, body.k, get_settings())
