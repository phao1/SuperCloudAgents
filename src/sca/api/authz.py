from collections.abc import Callable, Coroutine
from hmac import compare_digest
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request

from sca.core.errors import AuthError
from sca.core.models import Principal
from sca.db.repo import Store

ROLE_ORDER = {"viewer": 0, "member": 1, "admin": 2}
ACTION_ROLES = {
    "read": "viewer",
    "submit": "member",
    "upload": "member",
    "review": "member",
    "cancel": "member",
    "pause": "member",
    "resume": "member",
    "ci-result": "member",
    "tokens": "admin",
    "quota": "admin",
    "budget": "admin",
    "tenant-admin": "admin",
}
SCOPE_ACTIONS = {"ci:report": {"ci-result"}}


class ForbiddenError(Exception):
    pass


def _bearer(authorization: str | None) -> str | None:
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            return value
    return None


async def principal(request: Request, authorization: str | None = Header(None)) -> Principal:
    return await request.app.state.authenticator.authenticate(_bearer(authorization))


async def platform_admin_store(
    request: Request, authorization: str | None = Header(None),
) -> Store:
    token = _bearer(authorization)
    if not token:
        raise AuthError("缺少平台管理员令牌")
    configured = request.app.state.platform_admin_token
    if not configured or not compare_digest(configured.get_secret_value().encode(), token.encode()):
        raise ForbiddenError("需要平台管理员权限")
    return request.app.state.platform_store


def authorize(action: str) -> Callable[..., Coroutine[Any, Any, Principal]]:
    minimum = ACTION_ROLES[action]

    async def check(request: Request, user: Annotated[Principal, Depends(principal)]) -> Principal:
        if user.scopes:
            allowed = set().union(*(SCOPE_ACTIONS.get(scope, set()) for scope in user.scopes))
            if action not in allowed:
                raise ForbiddenError("Token scope does not allow this action")
        elif ROLE_ORDER[user.role] < ROLE_ORDER[minimum]:
            raise ForbiddenError("Insufficient role")
        if user.scopes and user.repo_url and action == "ci-result":
            task = await request.app.state.store.for_tenant(user.tenant_id).get_task(
                UUID(request.path_params["task_id"])
            )
            if task.spec.get("repo_url") != user.repo_url:
                raise ForbiddenError("Token is bound to another repository")
        if action == "submit":
            check_tenant = getattr(request.app.state, "check_tenant_submission", None)
            if check_tenant is not None:
                await check_tenant(user.tenant_id)
        return user

    return check
