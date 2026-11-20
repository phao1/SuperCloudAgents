from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from sca.api.authz import authorize
from sca.config import Settings
from sca.core.errors import InvalidTaskError, NotFoundError
from sca.core.models import Principal
from sca.scm.models import Integration, IntegrationInput, PullRequest
from sca.scm.provider import ScmError
from sca.scm.repository import provider_for_host

router = APIRouter(prefix="/v1")
_Admin = Annotated[Principal, Depends(authorize("tokens"))]
_Reader = Annotated[Principal, Depends(authorize("read"))]
_INTEGRATIONS = "/integrations/scm"
_INTEGRATION = _INTEGRATIONS + "/{integration_id}"


def _validate_integration(body: IntegrationInput, settings: Settings, tenant: str) -> dict:
    try:
        provider, _ = provider_for_host(settings, body.host)
    except ValueError:
        raise InvalidTaskError("SCM 主机未配置") from None
    if provider != body.provider:
        raise InvalidTaskError("SCM 主机与服务类型不匹配")
    secret = settings.scm_webhook_secrets.get(tenant, {}).get(body.webhook_secret_ref)
    if not secret or not secret.get_secret_value():
        raise InvalidTaskError("该租户的 webhook 密钥引用未配置")
    return body.model_dump(mode="json")


@router.post(_INTEGRATIONS, status_code=201)
async def create_integration(body: IntegrationInput, request: Request, user: _Admin) -> Integration:
    values = _validate_integration(body, request.app.state.scm_settings, user.tenant_id)
    store = request.app.state.store.for_tenant(user.tenant_id)
    return Integration.model_validate(await store.create_scm_integration(values))


@router.get(_INTEGRATIONS)
async def list_integrations(request: Request, user: _Admin) -> list[Integration]:
    store = request.app.state.store.for_tenant(user.tenant_id)
    return [Integration.model_validate(row) for row in await store.list_scm_integrations()]


@router.get(_INTEGRATION)
async def get_integration(integration_id: UUID, request: Request, user: _Admin) -> Integration:
    store = request.app.state.store.for_tenant(user.tenant_id)
    return Integration.model_validate(await store.get_scm_integration(integration_id))


@router.put(_INTEGRATION)
async def replace_integration(
    integration_id: UUID, body: IntegrationInput, request: Request, user: _Admin,
) -> Integration:
    values = _validate_integration(body, request.app.state.scm_settings, user.tenant_id)
    store = request.app.state.store.for_tenant(user.tenant_id)
    return Integration.model_validate(await store.replace_scm_integration(integration_id, values))


@router.delete(_INTEGRATION, status_code=204)
async def delete_integration(integration_id: UUID, request: Request, user: _Admin) -> Response:
    await request.app.state.store.for_tenant(user.tenant_id).delete_scm_integration(integration_id)
    return Response(status_code=204)


@router.get("/tasks/{task_id}/pr")
async def get_task_pr(task_id: UUID, request: Request, user: _Reader) -> PullRequest:
    store = request.app.state.store.for_tenant(user.tenant_id)
    task = await store.get_task(task_id, user.tenant_id)
    try:
        return await request.app.state.scm_service.refresh_pr(store, task)
    except ScmError as exc:
        if exc.status == 404:
            raise NotFoundError("代码托管服务未返回该 PR") from None
        raise HTTPException(502, str(exc)) from None
