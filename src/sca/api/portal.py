"""Non-secret runtime information for the local Portal."""

import asyncio
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import httpx
from dotenv import set_key
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, SecretStr
from sqlalchemy import func, select
from temporalio.service import RPCError

from sca.admission.order import tenant_slot_limit
from sca.agents.opencode_prompt import PROMPT_MODELS, PROMPT_PATH, PROMPT_SUITE
from sca.api.authz import ForbiddenError, authorize, platform_admin_store, principal
from sca.api.evals import can_rescore
from sca.config import get_settings
from sca.core.errors import AuthError, NotFoundError
from sca.core.models import Principal, TaskState
from sca.db.schema import plans, tasks, tenants, usage_daily
from sca.gateway.health import ProviderHealth

router = APIRouter()
_key_lock = asyncio.Lock()


class ProviderKey(BaseModel):
    key: SecretStr


@router.post('/v1/portal/provider-key')
async def set_provider_key(body: ProviderKey, request: Request,
                           user: Annotated[Principal, Depends(authorize('quota'))]) -> dict:
    """Configure the local demo gateway; this capability is opt-in and administrator-only."""
    config = os.environ.get('SCA_PORTAL_LOCAL_CONFIG')
    docker = os.environ.get('SCA_DOCKER_BIN')
    if not config or not docker or not request.client or request.client.host not in ('127.0.0.1', '::1'):
        raise HTTPException(status_code=403, detail='本地配置不可用')
    key = body.key.get_secret_value()
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', key):
        raise HTTPException(status_code=422, detail='密钥格式无效')
    path = Path(config)
    async with _key_lock:
        set_key(str(path), 'DEEPSEEK_API_KEY', key)
        path.chmod(0o600)
        try:
            process = await asyncio.create_subprocess_exec(
                docker, 'compose', '--env-file', str(path), '-f',
                str(path.parent / 'deploy/docker-compose.yml'), '--profile', 'gateway',
                'up', '-d', '--no-deps', '--force-recreate', 'litellm',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return {'saved': True, 'gateway_restarted': False}
        try:
            await asyncio.wait_for(process.wait(), 120)
        except TimeoutError:
            process.kill()
            await process.wait()
            return {'saved': True, 'gateway_restarted': False}
        return {'saved': True, 'gateway_restarted': process.returncode == 0}


async def gateway_models() -> tuple[bool, list[str]]:
    settings = get_settings()
    if not settings.model_gateway_url:
        return False, []
    async with httpx.AsyncClient(timeout=2, follow_redirects=False) as client:
        base = settings.model_gateway_url.rstrip("/")
        try:
            response = await client.get(base + "/health/liveliness")
            response.raise_for_status()
        except httpx.HTTPError:
            return False, []
        try:
            response = await client.get(
                base + "/v1/models",
                headers={"Authorization": "Bearer " + settings.model_gateway_master_key},
            )
            response.raise_for_status()
            names = [row["id"] for row in response.json().get("data", [])
                     if isinstance(row, dict) and isinstance(row.get("id"), str)]
            return True, names
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return True, []


async def temporal_reachable(request: Request) -> bool:
    client = getattr(request.app.state.orchestrator, "client", None)
    if client is None:
        return False
    try:
        async with asyncio.timeout(2):
            return bool(await client.service_client.check_health())
    except (TimeoutError, RuntimeError, RPCError, OSError):
        return False


@router.get("/v1/portal/config")
async def config(request: Request,
                 user: Annotated[Principal, Depends(authorize("read"))]) -> dict:
    settings = get_settings()
    (gateway, models), temporal = await asyncio.gather(gateway_models(), temporal_reachable(request))
    provider_health = ProviderHealth(settings.data_dir).read()
    backends = ["codex"]
    try:
        import sca.agents.opencode  # noqa: F401
    except ImportError:
        pass
    else:
        backends.extend(["opencode", "opencode-brain"])
    backends.extend(["pi", "dsh"])
    return {"agent_backends": backends, "default_model": settings.default_model,
            "default_agent_backend": settings.agent_backend,
            "models": models, "sandbox_driver": settings.sandbox_driver,
            "state_transport": settings.state_transport, "secret_provider": settings.secret_provider,
            "versions_dir": str(getattr(settings, "opencode_versions_dir",
                                        Path(settings.data_dir) / "opencode" / "versions")),
            "temporal_reachable": temporal, "gateway_reachable": gateway,
            "gateway_provider_error": provider_health.get("last_error"),
            "gateway_provider_account_error": provider_health.get("account_error"),
            "can_rescore": can_rescore(user),
            "prompt_evolution": {"suite": PROMPT_SUITE, "models": PROMPT_MODELS, "path": PROMPT_PATH},
            "can_configure_model": user.tenant_id == "eval" and user.role == "admin" and not user.scopes
            and bool(os.environ.get("SCA_PORTAL_LOCAL_CONFIG") and os.environ.get("SCA_DOCKER_BIN"))
            and bool(request.client and request.client.host in ("127.0.0.1", "::1"))}


# Phase 6 Portal

_ASSETS = {
    "portal-operations.css": "text/css",
    "portal-ui.js": "text/javascript",
    "portal-markdown.js": "text/javascript",
    "portal-tasks.js": "text/javascript",
    "portal-evolution.js": "text/javascript",
    "portal-settings.js": "text/javascript",
}


@router.get("/portal/assets/{name}", include_in_schema=False)
async def asset(name: str) -> FileResponse:
    if name not in _ASSETS:
        raise HTTPException(404, "页面资源不存在")
    return FileResponse(Path(__file__).parent / "static" / name, media_type=_ASSETS[name])


class PortalIdentity(BaseModel):
    tenant_id: str | None = None
    subject: str | None = None
    role: Literal["viewer", "member", "admin"] | None = None
    platform_admin: bool = False


@router.get("/v1/portal/whoami", response_model_exclude_none=True)
async def whoami(
    request: Request, authorization: Annotated[str | None, Header()] = None,
) -> PortalIdentity:
    try:
        await platform_admin_store(request, authorization)
    except (AuthError, ForbiddenError):
        user = await principal(request, authorization)
        if user.scopes:
            raise ForbiddenError("该令牌不支持 Portal 登录")
        return PortalIdentity(tenant_id=user.tenant_id, subject=user.subject, role=user.role)
    return PortalIdentity(platform_admin=True)


class TenantQuota(BaseModel):
    monthly_cost_usd: float
    monthly_cost_limit_usd: float | None
    max_task_cost_usd: float | None
    slots_used: int
    slots_limit: int
    queued: int
    queued_limit: int


@router.get("/v1/portal/tenant-quota")
async def tenant_quota(
    request: Request, user: Annotated[Principal, Depends(authorize("tenant-admin"))],
) -> TenantQuota:
    store = request.app.state.store.for_tenant(user.tenant_id)
    today = datetime.now(UTC).date()
    async with store._transaction() as session:
        plan = (await session.execute(
            select(plans, tenants.c.max_active_tasks)
            .join(tenants, tenants.c.plan == plans.c.name)
            .where(tenants.c.id == user.tenant_id, tenants.c.deleted_at.is_(None))
        )).mappings().one_or_none()
        if plan is None:
            raise NotFoundError("租户不存在")
        slots, queued = (await session.execute(
            select(func.count().filter(tasks.c.slot_held),
                   func.count().filter(tasks.c.state == TaskState.QUEUED))
            .where(tasks.c.tenant_id == user.tenant_id)
        )).one()
        spend = await session.scalar(
            select(func.coalesce(func.sum(usage_daily.c.cost_usd), 0)).where(
                usage_daily.c.tenant_id == user.tenant_id,
                usage_daily.c.day.between(today.replace(day=1), today),
            )
        )
    return TenantQuota(
        monthly_cost_usd=float(spend), monthly_cost_limit_usd=plan["monthly_cost_limit_usd"],
        max_task_cost_usd=plan["max_task_cost_usd"], slots_used=slots,
        slots_limit=tenant_slot_limit(plan["max_active_tasks"], plan["max_concurrent_tasks"]),
        queued=queued, queued_limit=request.app.state.settings.tenant_max_queued,
    )
