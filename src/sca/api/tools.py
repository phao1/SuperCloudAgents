from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import TypeAdapter, ValidationError

from sca.api.authz import ForbiddenError, authorize
from sca.core.errors import InvalidTaskError
from sca.core.models import Principal
from sca.db.repo import Store
from sca.tools.models import (
    KNOWLEDGE,
    PLATFORM,
    AvailableTool,
    GrantInput,
    GrantRecord,
    GrantType,
    Subject,
    ToolDefinition,
    ToolName,
    ToolPatch,
    ToolRecord,
)
from sca.tools.settings import get_tool_settings

router = APIRouter(prefix="/v1/tools")
Reader = Annotated[Principal, Depends(authorize("read"))]


async def require_admin(user: Reader) -> Principal:
    if user.role != "admin":
        raise ForbiddenError("需要租户管理员权限")
    return user


Admin = Annotated[Principal, Depends(require_admin)]


def _store(request: Request, user: Principal) -> Store:
    return request.app.state.store.for_tenant(user.tenant_id)


@router.get("/available")
async def available(request: Request, user: Reader) -> list[AvailableTool]:
    teams = get_tool_settings().teams_for(user.tenant_id, user.subject)
    return [AvailableTool.model_validate(row)
            for row in await _store(request, user).available_tools(user.subject, teams)]


@router.get("")
async def list_tools(request: Request, user: Admin) -> list[ToolRecord]:
    return [ToolRecord.model_validate(row) for row in await _store(request, user).list_tools()]


@router.post("", status_code=201)
async def create_tool(body: ToolDefinition, request: Request, user: Admin) -> ToolRecord:
    if body.kind == "builtin" and user.tenant_id != PLATFORM:
        raise ForbiddenError("只有平台管理员可以登记内置工具")
    return ToolRecord.model_validate(
        await _store(request, user).create_tool(body.model_dump(), user.subject)
    )


@router.get("/{name}")
async def get_tool(name: ToolName, request: Request, user: Admin) -> ToolRecord:
    return ToolRecord.model_validate(await _store(request, user).get_tool(name))


@router.patch("/{name}")
async def patch_tool(name: ToolName, body: ToolPatch, request: Request, user: Admin) -> ToolRecord:
    return ToolRecord.model_validate(
        await _store(request, user).patch_tool(name, body.model_dump(exclude_unset=True))
    )


@router.delete("/{name}", status_code=204)
async def delete_tool(name: ToolName, request: Request, user: Admin) -> Response:
    await _store(request, user).delete_tool(name)
    return Response(status_code=204)


async def _grant_scope(store: Store, name: str, tenant_id: str | None, *,
                       writing: bool = False) -> tuple[str, str]:
    tenant = tenant_id or store.tenant_id
    tool = await store.get_tool(name)
    owner = tool["tenant_id"]
    if store.tenant_id != PLATFORM and (
        tenant != store.tenant_id or (writing and owner == PLATFORM and name != KNOWLEDGE)
    ):
        raise ForbiddenError("该工具授权由所属租户管理员管理")
    assert tenant is not None
    return owner, tenant


@router.get("/{name}/grants")
async def list_grants(name: ToolName, request: Request, user: Admin,
                      tenant_id: Subject | None = None) -> list[GrantRecord]:
    store = _store(request, user)
    owner, tenant = await _grant_scope(store, name, tenant_id)
    return [GrantRecord.model_validate(row) for row in await store.list_tool_grants(owner, name, tenant)]


@router.post("/{name}/grants", status_code=201)
async def add_grant(name: ToolName, body: GrantInput, request: Request, user: Admin) -> GrantRecord:
    store = _store(request, user)
    owner, tenant = await _grant_scope(store, name, body.tenant_id, writing=True)
    if body.subject_type == "tenant" and body.subject != tenant:
        raise InvalidTaskError("租户授权的 subject 必须与目标租户一致")
    if body.subject_type == "team":
        try:
            TypeAdapter(ToolName).validate_python(body.subject)
        except ValidationError:
            raise InvalidTaskError("团队授权需要有效的团队名称") from None
    return GrantRecord.model_validate(await store.add_tool_grant(
        {**body.model_dump(), "tenant_id": tenant, "tool_tenant_id": owner, "tool_name": name},
        user.subject,
    ))


@router.delete("/{name}/grants", status_code=204)
async def remove_grant(name: ToolName, subject_type: GrantType, subject: Subject, request: Request,
                       user: Admin, tenant_id: Subject | None = None) -> Response:
    store = _store(request, user)
    owner, tenant = await _grant_scope(store, name, tenant_id, writing=True)
    await store.delete_tool_grant(owner, name, tenant, subject_type, subject)
    return Response(status_code=204)
