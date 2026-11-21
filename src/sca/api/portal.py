"""Configuration and read-only task progress for the local Portal."""

import asyncio
import os
import re
from pathlib import Path
from typing import Annotated
from uuid import UUID

import httpx
from dotenv import set_key
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, SecretStr
from temporalio.service import RPCError

from sca.agents.opencode_prompt import PROMPT_MODELS, PROMPT_PATH, PROMPT_SUITE
from sca.api.authz import authorize
from sca.api.evals import can_rescore
from sca.config import get_settings
from sca.core.models import Event, Principal, Task, TaskState
from sca.db.repo import Store

router = APIRouter(prefix="/v1/portal")
_key_lock = asyncio.Lock()
_DEMO_TENANT = "eval"
_LOCAL_CLIENTS = {"127.0.0.1", "::1"}
_AGENT_BACKENDS = {"codex", "fake", "opencode", "opencode-brain", "pi"}
_STATUS_FLAGS = {"passed", "skipped", "split", "pushed", "cache_hit", "mirror_only", "is_error"}
_PROGRESS_NUMBERS = {
    "turn", "attempt", "position", "exit_code", "wait_seconds", "cpus", "memory_mb",
    "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "cost",
    "duration_s", "duration_ms", "model_call_duration_ms", "started_at_ms", "ended_at_ms",
}
_PROGRESS_LABELS = {
    "driver": {"docker", "k8s"},
    "state_transport": {"mount", "sync"},
    "decision": {"approve", "request_changes", "reject"},
    "method": {"mount", "sync", "overlay", "clone", "reflink-auto", "all", "recent", "vector"},
    "workspace_method": {"overlay", "clone", "reflink-auto"},
    "status": {"merged", "unchanged", "conflict", "failed", "completed", "skipped"},
    "backend": _AGENT_BACKENDS,
    "from": set(TaskState),
    "to": set(TaskState),
}


def _local_progress_store(request: Request, response: Response) -> Store:
    if (
        not get_settings().portal_local_task_progress
        or request.client is None
        or request.client.host not in _LOCAL_CLIENTS
        or request.url.hostname not in _LOCAL_CLIENTS | {"localhost"}
    ):
        raise HTTPException(404, "本机任务进度未启用")
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.store.for_tenant(_DEMO_TENANT)


def _task_progress(task: Task) -> dict:
    spec = {key: bool(task.spec.get(key)) for key in ("auto_finish", "plan_review")}
    spec["plan"] = "auto" if task.spec.get("plan") == "auto" else "none"
    backend = task.spec.get("agent_backend")
    if isinstance(backend, str) and backend in _AGENT_BACKENDS:
        spec["agent_backend"] = backend
    result = {key: task.result[key] for key in ("verification_passed", "acceptance_passed")
              if type(task.result.get(key)) is bool}
    admission = task.result.get("admission")
    if isinstance(admission, dict) and type(admission.get("position")) is int:
        result["admission"] = {"position": admission["position"]}
    return {
        "id": task.id, "kind": task.kind, "state": task.state,
        "created_at": task.created_at, "updated_at": task.updated_at,
        "spec": spec, "result": result,
        "acceptance_configured": bool(task.spec.get("acceptance_files")),
    }


def _subtask_name(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", value))


def _progress_fields(payload: dict) -> dict:
    return {
        key: value for key, value in payload.items()
        if (key in _PROGRESS_NUMBERS and type(value) in (int, float))
        or (key in _STATUS_FLAGS and type(value) is bool)
        or (key in _PROGRESS_LABELS and isinstance(value, str) and value in _PROGRESS_LABELS[key])
    }


def _event_progress(event: Event) -> Event:
    # Keep event order and rendering metadata while omitting prompts, code and tool output.
    source = event.payload
    payload = _progress_fields(source)
    if _subtask_name(source.get("subtask")):
        payload["subtask"] = source["subtask"]
    for key in ("subtasks", "results"):
        if isinstance(items := source.get(key), list):
            payload[key] = [{"name": item["name"], **_progress_fields(item)} for item in items
                            if isinstance(item, dict) and _subtask_name(item.get("name"))]
    if isinstance(checks := source.get("checks"), list):
        payload["checks"] = [_progress_fields(check) for check in checks if isinstance(check, dict)]
    if event.type == "memory.injected" and isinstance(ids := source.get("ids"), list):
        payload["count"] = len(ids)
    for key in ("plan", "actor", "segment", "snapshot_id"):
        if source.get(key):
            payload[key] = True
    return event.model_copy(update={"payload": payload})


LocalProgressStore = Annotated[Store, Depends(_local_progress_store)]


@router.get("/tasks")
async def local_tasks(store: LocalProgressStore, limit: int = Query(50, ge=1, le=500)) -> list[dict]:
    return [_task_progress(task) for task in await store.list_tasks(_DEMO_TENANT, limit=limit)]


@router.get("/tasks/{task_id}")
async def local_task(task_id: UUID, store: LocalProgressStore) -> dict:
    return _task_progress(await store.get_task(task_id, _DEMO_TENANT))


@router.get("/tasks/{task_id}/events")
async def local_task_events(
    task_id: UUID, store: LocalProgressStore, after: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=500),
) -> list[Event]:
    return [_event_progress(event) for event in await store.list_events(task_id, after, limit)]


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
