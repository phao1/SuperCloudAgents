"""Tenant-scoped benchmark intake and platform-admin publication."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from sca.api.authz import authorize
from sca.api.evals import require_eval
from sca.config import get_settings
from sca.core.models import Principal
from sca.db.repo import Store
from sca.evolve.harvest import HarvestRequest, harvest, publish_case

router = APIRouter(prefix="/v1/evals/harvest")
Admin = Annotated[Principal, Depends(authorize("tokens"))]


def candidates(request: Request, user: Principal, tenant_id: str | None = None) -> Store:
    if tenant_id is not None and tenant_id != user.tenant_id:
        require_eval(user)
    return request.app.state.store.for_tenant(tenant_id or user.tenant_id)


@router.post("", status_code=201)
async def create(body: HarvestRequest, request: Request, user: Admin) -> dict:
    return await harvest(get_settings(), candidates(request, user), body, user.subject)


@router.get("")
async def listing(request: Request, user: Admin, tenant_id: str | None = None) -> list[dict]:
    return await candidates(request, user, tenant_id).list_harvest_candidates()


@router.post("/{candidate_id}/approve")
async def approve(
    candidate_id: UUID, request: Request, user: Admin, tenant_id: str | None = None,
) -> dict:
    require_eval(user)
    settings = get_settings()

    async def publish(row: dict) -> None:
        await asyncio.to_thread(publish_case, settings, row)

    return await candidates(request, user, tenant_id).review_harvest_candidate(
        candidate_id, "approved", user.subject, publish,
    )


@router.post("/{candidate_id}/reject")
async def reject(
    candidate_id: UUID, request: Request, user: Admin, tenant_id: str | None = None,
) -> dict:
    return await candidates(request, user, tenant_id).review_harvest_candidate(
        candidate_id, "rejected", user.subject,
    )
