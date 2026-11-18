"""Non-secret runtime information for the local Portal."""

import asyncio
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Request
from temporalio.service import RPCError

from sca.api.authz import authorize
from sca.api.evals import can_rescore
from sca.config import get_settings
from sca.core.models import Principal

router = APIRouter(prefix="/v1/portal")


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
            "can_rescore": can_rescore(user)}
