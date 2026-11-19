"""Non-secret runtime information for the local Portal."""

import asyncio
import os
import re
from pathlib import Path
from typing import Annotated

import httpx
from dotenv import set_key
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, SecretStr
from temporalio.service import RPCError

from sca.agents.opencode_prompt import PROMPT_MODELS, PROMPT_PATH, PROMPT_SUITE
from sca.api.authz import authorize
from sca.api.evals import can_rescore
from sca.config import get_settings
from sca.core.models import Principal

router = APIRouter(prefix="/v1/portal")
_key_lock = asyncio.Lock()


class ProviderKey(BaseModel):
    key: SecretStr


@router.post('/provider-key')
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


@router.get("/config")
async def config(request: Request,
                 user: Annotated[Principal, Depends(authorize("read"))]) -> dict:
    settings = get_settings()
    (gateway, models), temporal = await asyncio.gather(gateway_models(), temporal_reachable(request))
    backends = ["codex"]
    try:
        import sca.agents.opencode  # noqa: F401
    except ImportError:
        pass
    else:
        backends.extend(["opencode", "opencode-brain"])
    backends.append("pi")
    return {"agent_backends": backends, "default_model": settings.default_model,
            "models": models, "sandbox_driver": settings.sandbox_driver,
            "state_transport": settings.state_transport, "secret_provider": settings.secret_provider,
            "versions_dir": str(getattr(settings, "opencode_versions_dir",
                                        Path(settings.data_dir) / "opencode" / "versions")),
            "temporal_reachable": temporal, "gateway_reachable": gateway,
            "can_rescore": can_rescore(user),
            "prompt_evolution": {"suite": PROMPT_SUITE, "models": PROMPT_MODELS, "path": PROMPT_PATH},
            "can_configure_model": user.tenant_id == "eval" and user.role == "admin" and not user.scopes
            and bool(os.environ.get("SCA_PORTAL_LOCAL_CONFIG") and os.environ.get("SCA_DOCKER_BIN"))
            and bool(request.client and request.client.host in ("127.0.0.1", "::1"))}
