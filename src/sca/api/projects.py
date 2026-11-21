from contextlib import suppress
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from sca.api.authz import authorize
from sca.core.errors import ConflictError
from sca.core.models import Artifact, Principal
from sca.core.projects import (
    Project,
    ProjectAction,
    ProjectInput,
    ProjectOverview,
    ProjectPatch,
    ProjectUsage,
    Thread,
    ThreadGroup,
)
from sca.knowledge.models import DocumentMetadata
from sca.tools.settings import get_tool_settings

router = APIRouter(prefix="/v1/projects")
Reader = Annotated[Principal, Depends(authorize("read"))]
Writer = Annotated[Principal, Depends(authorize("review"))]
Admin = Annotated[Principal, Depends(authorize("tenant-admin"))]


class Library(BaseModel):
    documents: list[DocumentMetadata]
    artifacts: list[Artifact]


@router.get("")
async def list_projects(request: Request, user: Reader) -> list[ProjectOverview]:
    return await request.app.state.store.for_tenant(user.tenant_id).list_projects()


@router.post("", status_code=201)
async def create_project(body: ProjectInput, request: Request, user: Writer) -> Project:
    return await request.app.state.store.for_tenant(user.tenant_id).create_project(
        body.model_dump(), user.subject,
    )


@router.get("/{project_id}")
async def get_project(project_id: UUID, request: Request, user: Reader) -> Project:
    return await request.app.state.store.for_tenant(user.tenant_id).get_project(project_id)


@router.patch("/{project_id}")
async def patch_project(
    project_id: UUID, body: ProjectPatch, request: Request, user: Writer,
) -> Project:
    return await request.app.state.store.for_tenant(user.tenant_id).update_project(
        project_id, body.model_dump(exclude_unset=True),
    )


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: UUID, request: Request, user: Admin) -> Response:
    store = request.app.state.store.for_tenant(user.tenant_id)
    for task_id in await store.delete_project(project_id):
        with suppress(ConflictError):
            await request.app.state.orchestrator.cancel_task(str(task_id))
    return Response(status_code=204)


async def _change_status(
    project_id: UUID, action: ProjectAction, request: Request, user: Principal,
) -> Project:
    return await request.app.state.store.for_tenant(user.tenant_id).change_project_status(
        project_id, action,
    )


@router.post("/{project_id}/pause")
async def pause(project_id: UUID, request: Request, user: Writer) -> Project:
    return await _change_status(project_id, "pause", request, user)


@router.post("/{project_id}/resume")
async def resume(project_id: UUID, request: Request, user: Writer) -> Project:
    return await _change_status(project_id, "resume", request, user)


@router.post("/{project_id}/archive")
async def archive(project_id: UUID, request: Request, user: Writer) -> Project:
    return await _change_status(project_id, "archive", request, user)


@router.post("/{project_id}/unarchive")
async def unarchive(project_id: UUID, request: Request, user: Writer) -> Project:
    return await _change_status(project_id, "unarchive", request, user)


@router.get("/{project_id}/threads")
async def list_threads(
    project_id: UUID, request: Request, user: Reader, group: ThreadGroup | None = None,
) -> list[Thread]:
    return await request.app.state.store.for_tenant(user.tenant_id).list_project_threads(
        project_id, group,
    )


@router.get("/{project_id}/usage")
async def usage(project_id: UUID, request: Request, user: Reader) -> ProjectUsage:
    store = request.app.state.store.for_tenant(user.tenant_id)
    return await store.project_usage(project_id, request.app.state.settings.default_model)


@router.get("/{project_id}/library")
async def library(
    project_id: UUID, request: Request, user: Reader,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Library:
    store = request.app.state.store.for_tenant(user.tenant_id)
    artifacts = await store.project_artifacts(project_id)
    teams = get_tool_settings().teams_for(user.tenant_id, user.subject)
    documents = await store.list_knowledge_documents(teams, limit, offset, project_id=project_id)
    return Library(documents=documents, artifacts=artifacts)
