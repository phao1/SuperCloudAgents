from collections.abc import Callable, Coroutine
from hmac import compare_digest
from ipaddress import IPv6Address, ip_address
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, Header, Request

from sca.core.errors import AuthError
from sca.core.models import Principal
from sca.db.repo import Store
from sca.gateway.configuration import demo_config_path

ROLE_ORDER = {"viewer": 0, "member": 1, "admin": 2}
ACTION_ROLES = {
    "read": "viewer",
    "submit": "member",
    "upload": "member",
    "review": "member",
    "task-message": "member",
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


def authorize(action: str, *, guest: bool = False) -> Callable[..., Coroutine[Any, Any, Principal]]:
    minimum = ACTION_ROLES[action]
    authenticate = history_principal if guest else principal

    async def check(request: Request, user: Annotated[Principal, Depends(authenticate)]) -> Principal:
        if isinstance(user, GuestPrincipal):
            if guest and request.method == "GET":
                return user
            raise ForbiddenError("访客只能浏览历史")
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


# Guest history
class GuestPrincipal(Principal):
    role: Literal["viewer"] = "viewer"
    subject: Literal["guest"] = "guest"


def is_loopback(host: str) -> bool:
    try:
        address = ip_address(host)
    except ValueError:
        return False
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


async def history_principal(request: Request, authorization: str | None = Header(None)) -> Principal:
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and request.method == "GET" and authorization is None:
        mode = settings.guest_view
        if mode == "all" or (mode == "local" and request.client and is_loopback(request.client.host)):
            tenant = settings.guest_tenant or ("eval" if demo_config_path() else "default")
            if not await request.app.state.store.tenant_deleted(tenant):
                return GuestPrincipal(tenant_id=tenant)
    return await principal(request, authorization)
