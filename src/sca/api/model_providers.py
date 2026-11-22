"""Manage the installation's LiteLLM model routes from the local Portal."""

from time import monotonic
from typing import Annotated
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from sca.api.portal import model_admin, model_config_lock, restart_gateway
from sca.core.model_provider import classify_model_error
from sca.gateway.configuration import deepseek_configured, demo_config_path
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


class ProviderChange(BaseModel):
    provider: Provider | None = None
    gateway_restarted: bool


class ConnectionInput(BaseModel):
    model: ModelName


class ConnectionResult(BaseModel):
    ok: bool
    latency_ms: int
    message: str


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


async def apply_registry(request: Request, registry: ProviderRegistry, command: list[str]) -> bool:
    try:
        registry.save(request.app.state.settings.data_dir)
    except OSError:
        raise HTTPException(503, "无法保存本机模型接入配置") from None
    return await restart_gateway(command)


@router.get("", dependencies=[Depends(model_admin)])
async def list_providers(request: Request) -> ProviderList:
    return ProviderList(providers=registry_for(request).providers,
                        deepseek_models=list(DEEPSEEK_MODELS) if configured_deepseek() else [])


@router.get("/{provider_id}", dependencies=[Depends(model_admin)])
async def get_provider(provider_id: UUID, request: Request) -> Provider:
    return find_provider(registry_for(request), provider_id)


async def save_provider(request: Request, command: list[str], body: ProviderInput,
                        *, provider_id: UUID | None = None) -> ProviderChange:
    async with model_config_lock:
        registry = registry_for(request)
        if provider_id is None:
            provider_id = uuid4()
        else:
            find_provider(registry, provider_id)
        try:
            updated = registry.with_provider(provider_id, body)
        except RegistryError as exc:
            raise HTTPException(409, str(exc)) from None
        restarted = await apply_registry(request, updated, command)
        return ProviderChange(provider=find_provider(updated, provider_id), gateway_restarted=restarted)


@router.post("", status_code=201)
async def add_provider(body: ProviderInput, request: Request, command: ModelAdmin) -> ProviderChange:
    return await save_provider(request, command, body)


@router.put("/{provider_id}")
async def update_provider(provider_id: UUID, body: ProviderInput, request: Request,
                          command: ModelAdmin) -> ProviderChange:
    return await save_provider(request, command, body, provider_id=provider_id)


@router.delete("/{provider_id}", response_model_exclude_none=True)
async def delete_provider(provider_id: UUID, request: Request, command: ModelAdmin) -> ProviderChange:
    async with model_config_lock:
        registry = registry_for(request)
        updated = registry.without_provider(find_provider(registry, provider_id))
        return ProviderChange(gateway_restarted=await apply_registry(request, updated, command))


@router.post("/test", dependencies=[Depends(model_admin)])
async def test_connection(body: ConnectionInput, request: Request) -> ConnectionResult:
    if body.model not in registry_for(request).models and not (
        body.model in DEEPSEEK_MODELS and configured_deepseek()
    ):
        raise HTTPException(404, "请先保存要测试的模型")
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
