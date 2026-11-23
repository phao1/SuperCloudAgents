"""Manage the installation's LiteLLM model routes from the local Portal."""

from time import monotonic
from typing import Annotated, Literal
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool

from sca.api.portal import gateway_restarts, model_admin
from sca.core.model_provider import classify_model_error
from sca.gateway.configuration import active_model_names, deepseek_configured, demo_config_path
from sca.gateway.model_providers import (
    DEEPSEEK_MODELS,
    ModelName,
    Provider,
    ProviderInput,
    ProviderRegistry,
    RegistryError,
    read_registry,
)

router = APIRouter(prefix="/v1/portal/providers")
ModelAdmin = Annotated[list[str], Depends(model_admin)]


class ProviderList(BaseModel):
    providers: list[Provider]
    deepseek_models: list[str]
    deepseek_has_key: bool
    deepseek_enabled: bool
    default_model: ModelName | None


class RestartStatus(BaseModel):
    id: UUID
    status: Literal["restarting", "succeeded", "failed"]


class ProviderChange(BaseModel):
    provider: Provider | None = None
    restart: RestartStatus


class ConnectionInput(BaseModel):
    model: ModelName


class ConnectionResult(BaseModel):
    ok: bool
    latency_ms: int
    message: str


class ProviderStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class DefaultModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: ModelName | None


def configured_deepseek() -> bool:
    path = demo_config_path()
    return path is not None and deepseek_configured(path)


def registry_for(request: Request) -> ProviderRegistry:
    try:
        return read_registry(request.app.state.settings.data_dir)
    except RegistryError as exc:
        raise HTTPException(503, str(exc)) from None


def find_provider(registry: ProviderRegistry, provider_id: UUID) -> Provider:
    for row in registry.providers:
        if row.id == provider_id:
            return row
    raise HTTPException(404, "供应商不存在")


def save_registry(request: Request, registry: ProviderRegistry) -> None:
    try:
        registry.save(request.app.state.settings.data_dir)
    except OSError:
        raise HTTPException(503, "无法保存本机模型接入配置") from None


def apply_registry(request: Request, registry: ProviderRegistry, command: list[str]) -> RestartStatus:
    save_registry(request, registry)
    restart_id, _ = gateway_restarts(request).start(command)
    return RestartStatus(id=restart_id, status="restarting")


@router.get("", dependencies=[Depends(model_admin)])
async def list_providers(request: Request) -> ProviderList:
    registry = registry_for(request)
    has_key = configured_deepseek()
    return ProviderList(providers=registry.providers, default_model=registry.default_model,
                        deepseek_enabled=registry.deepseek_enabled, deepseek_has_key=has_key,
                        deepseek_models=list(DEEPSEEK_MODELS) if registry.deepseek_enabled and has_key else [])


@router.put("/default", dependencies=[Depends(model_admin)])
async def set_default_model(body: DefaultModel, request: Request) -> DefaultModel:
    gateway_restarts(request).require_idle()
    registry = registry_for(request)
    if body.model is not None and body.model not in active_model_names(registry, demo_config_path()):
        raise HTTPException(422, "请选择已启用的模型")
    save_registry(request, registry.model_copy(update={"default_model": body.model}))
    return body


@router.put("/deepseek/enabled", status_code=202, response_model_exclude_none=True)
async def set_deepseek_enabled(body: ProviderStatus, request: Request, command: ModelAdmin) -> ProviderChange:
    gateway_restarts(request).require_idle()
    if body.enabled and not configured_deepseek():
        raise HTTPException(409, "请先填写 DeepSeek API Key")
    updated = registry_for(request).model_copy(update={"deepseek_enabled": body.enabled})
    return ProviderChange(restart=apply_registry(request, updated, command))


@router.put("/{provider_id}/enabled", status_code=202)
async def set_provider_enabled(provider_id: UUID, body: ProviderStatus, request: Request,
                               command: ModelAdmin) -> ProviderChange:
    gateway_restarts(request).require_idle()
    registry = registry_for(request)
    updated = registry.with_provider_enabled(find_provider(registry, provider_id), body.enabled)
    restart = apply_registry(request, updated, command)
    return ProviderChange(provider=find_provider(updated, provider_id), restart=restart)


@router.get("/{provider_id}", dependencies=[Depends(model_admin)])
async def get_provider(provider_id: UUID, request: Request) -> Provider:
    return find_provider(registry_for(request), provider_id)


def save_provider(request: Request, command: list[str], body: ProviderInput,
                  *, provider_id: UUID | None = None) -> ProviderChange:
    # Check, save and schedule the restart in one event loop turn.
    gateway_restarts(request).require_idle()
    registry = registry_for(request)
    if provider_id is None:
        provider_id = uuid4()
    else:
        find_provider(registry, provider_id)
    try:
        updated = registry.with_provider(provider_id, body)
    except RegistryError as exc:
        raise HTTPException(409, str(exc)) from None
    restart = apply_registry(request, updated, command)
    return ProviderChange(provider=find_provider(updated, provider_id), restart=restart)


@router.post("", status_code=202)
async def add_provider(body: ProviderInput, request: Request, command: ModelAdmin) -> ProviderChange:
    return save_provider(request, command, body)


@router.put("/{provider_id}", status_code=202)
async def update_provider(provider_id: UUID, body: ProviderInput, request: Request,
                          command: ModelAdmin) -> ProviderChange:
    return save_provider(request, command, body, provider_id=provider_id)


@router.delete("/{provider_id}", status_code=202, response_model_exclude_none=True)
async def delete_provider(provider_id: UUID, request: Request, command: ModelAdmin) -> ProviderChange:
    gateway_restarts(request).require_idle()
    registry = registry_for(request)
    updated = registry.without_provider(find_provider(registry, provider_id))
    return ProviderChange(restart=apply_registry(request, updated, command))


@router.get("/restarts/{restart_id}", dependencies=[Depends(model_admin)])
async def get_restart(restart_id: UUID, request: Request, response: Response) -> RestartStatus:
    job = gateway_restarts(request).jobs.get(restart_id)
    if job is None:
        raise HTTPException(404, "找不到网关重启记录，请检查网关状态。")
    response.headers["Cache-Control"] = "no-store"
    if not job.done():
        return RestartStatus(id=restart_id, status="restarting")
    succeeded = not job.cancelled() and job.exception() is None and job.result()
    return RestartStatus(id=restart_id, status="succeeded" if succeeded else "failed")


@router.post("/test", dependencies=[Depends(model_admin)])
async def test_connection(body: ConnectionInput, request: Request) -> ConnectionResult:
    gateway_restarts(request).require_idle()
    if body.model not in active_model_names(registry_for(request), demo_config_path()):
        raise HTTPException(404, "请先保存并启用要测试的模型")
    settings = request.app.state.settings
    if not settings.model_gateway_url:
        raise HTTPException(503, "模型网关未配置")
    started = monotonic()

    def report(ok: bool, message: str) -> ConnectionResult:
        return ConnectionResult(ok=ok, latency_ms=round((monotonic() - started) * 1000), message=message)

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.post(
                settings.model_gateway_url.rstrip("/") + "/v1/chat/completions",
                headers={"Authorization": "Bearer " + settings.model_gateway_master_key},
                json={"model": body.model, "messages": [{"role": "user", "content": "Reply OK."}],
                      "max_tokens": 16},
            )
        if not response.is_success:
            # The upstream may echo headers and prompts. Return only fixed messages and the status.
            error = classify_model_error(response.text, status=response.status_code)
            label = {"auth": "模型认证失败", "billing": "模型账户额度不足", "rate_limit": "模型请求受限",
                     "unavailable": "模型服务暂不可达"}.get(error.kind if error else "", "模型请求失败")
            return report(False, f"{label}（HTTP {response.status_code}），请检查网关与供应商配置")
        message = response.json()["choices"][0]["message"]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("invalid completion")
    except httpx.TimeoutException:
        return report(False, "连接测试超时，请检查模型服务与网络")
    except httpx.HTTPError:
        return report(False, "无法连接模型网关")
    except (ValueError, TypeError, KeyError, IndexError):
        return report(False, "模型网关返回了无效的补全结果")
    return report(True, "测试连接成功")
