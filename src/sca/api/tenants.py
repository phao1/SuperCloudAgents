from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from sca.api.authz import authorize, platform_admin_store
from sca.api.routes import TokenRequest, create_token, list_tokens, revoke_token
from sca.core.models import Principal
from sca.db.repo import Store
from sca.tenancy.models import (
    MemberInvite,
    PlatformMemberInvite,
    TenantCreate,
    TenantNameUpdate,
    TenantUpdate,
)

router = APIRouter(prefix="/v1")
PlatformStore = Annotated[Store, Depends(platform_admin_store)]
TenantAdmin = Annotated[Principal, Depends(authorize("tenant-admin"))]


@router.get("/admin/tenants")
async def list_tenants(store: PlatformStore) -> list[dict[str, Any]]:
    return await store.list_tenants()


@router.post("/admin/tenants", status_code=201)
async def create_tenant(body: TenantCreate, store: PlatformStore) -> dict[str, Any]:
    return await store.create_tenant(body.id, body.display_name, body.plan, body.owner_email)


@router.get("/admin/tenants/{tenant_id}")
async def get_tenant(tenant_id: str, store: PlatformStore) -> dict[str, Any]:
    return await store.get_tenant(tenant_id)


@router.patch("/admin/tenants/{tenant_id}")
async def update_tenant(tenant_id: str, body: TenantUpdate, store: PlatformStore) -> dict[str, Any]:
    return await store.update_tenant(tenant_id, **body.model_dump(exclude_none=True))


@router.delete("/admin/tenants/{tenant_id}", status_code=204)
async def delete_tenant(tenant_id: str, store: PlatformStore) -> Response:
    await store.delete_tenant(tenant_id)
    return Response(status_code=204)


@router.post("/admin/tenants/{tenant_id}/suspend")
async def suspend_tenant(tenant_id: str, store: PlatformStore) -> dict[str, Any]:
    return await store.update_tenant(tenant_id, status="suspended")


@router.post("/admin/tenants/{tenant_id}/resume")
async def resume_tenant(tenant_id: str, store: PlatformStore) -> dict[str, Any]:
    return await store.update_tenant(tenant_id, status="active")


@router.post("/admin/tenants/{tenant_id}/members", status_code=201)
async def invite_for_tenant(
    tenant_id: str, body: PlatformMemberInvite, store: PlatformStore,
) -> dict[str, Any]:
    return await store.for_tenant(tenant_id).invite_member(body.email, body.role)


@router.post("/admin/tenants/{tenant_id}/tokens", status_code=201)
async def issue_tenant_token(
    tenant_id: str, body: TokenRequest, store: PlatformStore, response: Response,
) -> dict[str, Any]:
    """Bootstrap a tenant without OIDC: the operator issues its first API token."""
    import hashlib
    import secrets
    from datetime import UTC, datetime, timedelta

    await store.get_tenant(tenant_id)  # 404 for unknown tenants
    response.headers["Cache-Control"] = "no-store"
    token = "sca_" + secrets.token_urlsafe(32)
    row = await store.for_tenant(tenant_id).create_api_token(
        name=body.name, role=body.role or "admin", scopes=body.scopes, repo_url=body.repo_url,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(days=body.days), created_by="platform-admin",
    )
    return {**row, "token": token}


@router.get("/admin/plans")
async def list_plans(store: PlatformStore) -> list[dict[str, Any]]:
    from sca.billing.report import _json_numbers

    return [_json_numbers(plan) for plan in await store.list_plans()]


@router.get("/tenant")
async def my_tenant(request: Request, user: TenantAdmin) -> dict[str, Any]:
    return await request.app.state.store.for_tenant(user.tenant_id).get_tenant(user.tenant_id)


@router.patch("/tenant")
async def rename_tenant(body: TenantNameUpdate, request: Request, user: TenantAdmin) -> dict[str, Any]:
    return await request.app.state.store.for_tenant(user.tenant_id).update_tenant(
        user.tenant_id, display_name=body.display_name)


@router.get("/tenant/members")
async def list_members(request: Request, user: TenantAdmin) -> list[dict[str, Any]]:
    return await request.app.state.store.for_tenant(user.tenant_id).list_members()


@router.post("/tenant/members", status_code=201)
async def invite_member(body: MemberInvite, request: Request, user: TenantAdmin) -> dict[str, Any]:
    return await request.app.state.store.for_tenant(user.tenant_id).invite_member(body.email, body.role)


@router.post("/tenant/tokens", status_code=201)
async def create_tenant_token(
    body: TokenRequest, request: Request, response: Response, user: TenantAdmin,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await create_token(body, request, user)


router.add_api_route("/tenant/tokens", list_tokens, methods=["GET"])
router.add_api_route("/tenant/tokens/{token_id}", revoke_token, methods=["DELETE"], status_code=204)
