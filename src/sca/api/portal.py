"""Non-secret runtime information for the local Portal."""

import asyncio
import os
import re
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

import httpx
from dotenv import set_key
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, SecretStr
from sqlalchemy import func, select
from temporalio.service import RPCError

from sca.admission.order import tenant_slot_limit
from sca.agents.opencode_prompt import PROMPT_PATH, PROMPT_SUITE
from sca.api.authz import (
    ForbiddenError,
    GuestPrincipal,
    LocalPortalPrincipal,
    PortalAccessSetting,
    authorize,
    local_portal_admin,
    local_portal_host,
    platform_admin_store,
    portal_token_required,
    principal,
)
from sca.api.evals import can_rescore
from sca.api.portal_runtime import router as runtime_router
from sca.config import SOURCE_MODE_WARNING, Settings, get_settings
from sca.core.errors import AuthError, NotFoundError
from sca.core.models import Principal, TaskState
from sca.db.schema import plans, tasks, tenants, usage_daily
from sca.gateway.configuration import demo_config_path, model_configured, prefer_model
from sca.gateway.health import ProviderHealth
from sca.gateway.model_providers import read_registry

router = APIRouter()


async def model_admin(request: Request,
                      user: Annotated[Principal, Depends(authorize('quota'))]) -> list[str]:
    if user.tenant_id != 'eval':
        raise HTTPException(403, '模型接入需要 eval 租户的管理员令牌。')
    if not _local_client(request):
        raise HTTPException(403, '模型接入仅允许从 Portal 所在的本机访问。')
    command = _restart_gateway_command(os.environ.get('SCA_PORTAL_LOCAL_CONFIG'))
    if not command:
        raise HTTPException(403, '缺少本机模型接入配置，请通过 Portal 启动脚本运行 up。')
    return command


class ProviderKey(BaseModel):
    key: SecretStr


@router.post('/v1/portal/provider-key')
async def set_provider_key(
    body: ProviderKey, request: Request, command: Annotated[list[str], Depends(model_admin)],
) -> dict:
    """Configure the local demo gateway; this capability is opt-in and administrator-only."""
    key = body.key.get_secret_value()
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', key):
        raise HTTPException(status_code=422, detail='密钥格式无效')
    return await _write_provider_key(key, request, command)


@router.delete('/v1/portal/provider-key')
async def clear_provider_key(request: Request, command: Annotated[list[str], Depends(model_admin)]) -> dict:
    return await _write_provider_key('REPLACE_ME', request, command)


async def _write_provider_key(key: str, request: Request, command: list[str]) -> dict:
    path = demo_config_path()
    if path is None or not command:
        raise HTTPException(status_code=403, detail='本地配置不可用')
    restarts = gateway_restarts(request)
    restarts.require_idle()
    set_key(str(path), 'DEEPSEEK_API_KEY', key)
    path.chmod(0o600)
    _, restart = restarts.start(command)
    return {'saved': True, 'gateway_restarted': await asyncio.shield(restart)}


async def restart_gateway(command: list[str]) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        await asyncio.wait_for(process.wait(), 120)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return False
    return process.returncode == 0


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


# Model catalog. The Portal exposes aliases and limits independently from provider administration.
def model_catalog(settings: Settings, models: list[str]) -> dict:
    registry = read_registry(settings.data_dir)
    configured = registry.models
    preferred = registry.default_model if demo_config_path() and registry.default_model in models else None
    return {
        "data": [{"id": name,
                  "context_window": configured[name].context_window if name in configured else None,
                  "unpriced": name in configured and configured[name].unpriced} for name in models],
        "default_model": prefer_model(preferred or settings.default_model, models),
        "evolve_model": prefer_model(settings.evolve_model, models),
    }


@router.get("/v1/models")
async def models_catalog(user: Annotated[Principal, Depends(authorize("read"))]) -> dict:
    _, models = await gateway_models()
    return model_catalog(get_settings(), models)


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
                 user: Annotated[Principal, Depends(authorize("read", guest=True))]) -> dict:
    configured = model_configured()
    if isinstance(user, GuestPrincipal):
        return {"guest_view": True, "guest_tenant": user.tenant_id, "model_configured": configured}
    settings = get_settings()
    (gateway, models), temporal = await asyncio.gather(gateway_models(), temporal_reachable(request))
    catalog = model_catalog(settings, models)
    provider_health = ProviderHealth(settings.data_dir).read()
    backends = ["codex"]
    try:
        import sca.agents.opencode  # noqa: F401
    except ImportError:
        pass
    else:
        backends.extend(["opencode", "opencode-brain"])
    backends.extend(["pi", "dsh"])
    return {"guest_view": False, "model_configured": configured,
            "agent_backends": backends, "default_model": catalog["default_model"],
            "evolve_model": catalog["evolve_model"],
            "unpriced_models": [row["id"] for row in catalog["data"] if row["unpriced"]],
            "default_agent_backend": settings.agent_backend,
            "max_parallel_agents": settings.effective_max_parallel_agents,
            "models": models, "sandbox_driver": settings.sandbox_driver,
            "state_transport": settings.state_transport, "secret_provider": settings.secret_provider,
            "versions_dir": str(getattr(settings, "opencode_versions_dir",
                                        Path(settings.data_dir) / "opencode" / "versions")),
            "temporal_reachable": temporal, "gateway_reachable": gateway,
            "gateway_provider_error": provider_health.get("last_error"),
            "gateway_provider_account_error": provider_health.get("account_error"),
            "can_rescore": can_rescore(user),
            "prompt_evolution": {"suite": PROMPT_SUITE, "models": models, "path": PROMPT_PATH},
            "can_configure_model": user.tenant_id == "eval" and user.role == "admin" and not user.scopes
            and bool(_restart_gateway_command(os.environ.get("SCA_PORTAL_LOCAL_CONFIG")))
            and _local_client(request),
            "model_docker_mode": os.environ.get("SCA_RUNTIME_MODE") == "docker",
            **_portal_access_config(request, user),
            **(_source_config(settings) if settings.sandbox_driver == "local" else {})}


# Phase 6 Portal

_ASSETS = {
    "portal-operations.css": "text/css",
    "portal-navigation.css": "text/css",
    "portal-ui.js": "text/javascript",
    "portal-nav.js": "text/javascript",
    "portal-home.js": "text/javascript",
    "portal-markdown.js": "text/javascript",
    "portal-tasks.js": "text/javascript",
    "portal-evolution.js": "text/javascript",
    "portal-conversation.js": "text/javascript",
    "portal-conversation.css": "text/css",
    "portal-settings.js": "text/javascript",
    "portal-projects.js": "text/javascript",
    "portal-models.js": "text/javascript",
    "portal-runtime.js": "text/javascript",
    "task-dag.js": "text/javascript",
    "task-dag.css": "text/css",
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


# Local source mode
def _source_config(settings: Settings) -> dict:
    from sca.agents.local import SOURCE_BACKENDS, host_agent_binary, unavailable_source_backend

    backends, unavailable = [], {}
    for name in SOURCE_BACKENDS:
        if host_agent_binary(settings, name) is not None:
            backends.append(name)
        else:
            unavailable[name] = unavailable_source_backend(name)
    if settings.agent_backend == "fake":
        backends.append("fake")
    return {"agent_backends": backends, "unavailable_agent_backends": unavailable,
            "warning": SOURCE_MODE_WARNING}


def _restart_gateway_command(config: str | None) -> list[str]:
    if not config or (os.environ.get("SCA_RUNTIME_MODE") != "source" and not os.environ.get("SCA_DOCKER_BIN")):
        return []
    root = Path(config).parent
    if sys.platform == "win32" and os.environ.get("SCA_RUNTIME_MODE") == "source":
        return [sys.executable, str(root / "scripts/portal_source.py"), "restart-gateway"]
    return ["bash", str(root / "scripts/portal_demo.sh"), "restart-gateway"]


def _local_client(request: Request) -> bool:
    return bool(request.client and request.client.host in ("127.0.0.1", "::1"))


# Provider gateway restarts
class GatewayRestarts:
    def __init__(self) -> None:
        self.jobs: dict[UUID, asyncio.Task[bool]] = {}

    def require_idle(self) -> None:
        if self.jobs and not self.jobs[next(reversed(self.jobs))].done():
            raise HTTPException(409, "模型网关正在重启，请等待重启完成后重试。")

    def start(self, command: list[str]) -> tuple[UUID, asyncio.Task[bool]]:
        restart_id = uuid4()
        job = asyncio.create_task(restart_gateway(command))
        self.jobs[restart_id] = job
        if len(self.jobs) > 32:
            del self.jobs[next(iter(self.jobs))]
        return restart_id, job


def gateway_restarts(request: Request) -> GatewayRestarts:
    state = request.app.state
    if not hasattr(state, "gateway_restarts"):
        state.gateway_restarts = GatewayRestarts()
    return state.gateway_restarts


# Local Portal authentication
class PortalAccessStatus(PortalAccessSetting):
    local_no_token_active: bool


def _can_manage_portal_access(request: Request, user: Principal) -> bool:
    return (user.tenant_id == "eval" and user.role == "admin" and not user.scopes
            and local_portal_host(request) is not None)


def _portal_access_config(request: Request, user: Principal) -> dict:
    if not _can_manage_portal_access(request, user):
        return {}
    config: dict = {"portal_require_token": portal_token_required(request.app.state.settings)}
    if isinstance(user, LocalPortalPrincipal):
        config["local_identity"] = PortalIdentity(tenant_id=user.tenant_id, subject=user.subject, role=user.role)
    return config


@router.get("/v1/portal/access")
async def get_portal_access(request: Request, response: Response) -> PortalAccessStatus:
    response.headers["Cache-Control"] = "no-store"
    required = local_portal_host(request) is None or portal_token_required(request.app.state.settings)
    active = not required and await local_portal_admin(request) is not None
    return PortalAccessStatus(require_token=required, local_no_token_active=active)


@router.put("/v1/portal/access")
async def set_portal_access(
    body: PortalAccessSetting, request: Request, response: Response,
    user: Annotated[Principal, Depends(authorize("tenant-admin"))],
) -> PortalAccessStatus:
    if not _can_manage_portal_access(request, user):
        raise ForbiddenError("仅本机 Portal 的 eval 管理员可修改访问令牌开关。")
    active = not body.require_token and await local_portal_admin(request) is not None
    try:
        body.save(request.app.state.settings)
    except OSError:
        raise HTTPException(503, "无法保存本机 Portal 访问设置。") from None
    response.headers["Cache-Control"] = "no-store"
    return PortalAccessStatus(require_token=body.require_token, local_no_token_active=active)


# Portal runtime switching
router.include_router(runtime_router)
