from collections.abc import Callable, Coroutine
from hmac import compare_digest
from ipaddress import IPv6Address, ip_address
from os import environ
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, Header, Request
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError

from sca.config import Settings
from sca.core.errors import AuthError
from sca.core.models import Principal
from sca.db.repo import Store
from sca.gateway.configuration import demo_config_path
from sca.gateway.model_providers import atomic_write

ROLE_ORDER = {"viewer": 0, "member": 1, "admin": 2}
ACTION_ROLES = {
    "read": "viewer",
    "submit": "member",
    "upload": "member",
    "review": "member",
    "task-message": "member",
    "evolution-message": "member",
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
    return await _request_principal(request, authorization, guest=False)


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
    return await _request_principal(request, authorization, guest=True)


def _default_history_tenant() -> str:
    # The demo launcher exports its runtime mode even with older configs.
    demo = demo_config_path() is not None or environ.get("SCA_RUNTIME_MODE") in {"docker", "source"}
    return "eval" if demo else "default"


async def _guest_principal(request: Request) -> GuestPrincipal | None:
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and request.method == "GET":
        mode = settings.guest_view
        if mode == "all" or (mode == "local" and request.client and is_loopback(request.client.host)):
            tenant = settings.guest_tenant or _default_history_tenant()
            if not await request.app.state.store.tenant_deleted(tenant):
                return GuestPrincipal(tenant_id=tenant)
    return None


# Local Portal authentication
_PORTAL_ACCESS_FILE = "portal-access.json"


class LocalPortalPrincipal(Principal):
    pass


class PortalAccessSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    require_token: StrictBool

    def save(self, settings: Settings) -> None:
        atomic_write(settings.data_dir / _PORTAL_ACCESS_FILE, self.model_dump_json() + "\n")


def portal_token_required(settings: Settings) -> bool:
    try:
        data = (settings.data_dir / _PORTAL_ACCESS_FILE).read_bytes()
        return PortalAccessSetting.model_validate_json(data).require_token
    except FileNotFoundError:
        return settings.portal_require_token
    except (OSError, ValidationError):
        return True


def local_portal_host(request: Request) -> str | None:
    # Only the Portal launcher can attest to the listening address and proxy configuration.
    port = getattr(request.app.state, "local_portal_port", None)
    server = request.scope.get("server")
    if (port is None or not server or server[1] != port or not is_loopback(server[0])
            or not request.client or not is_loopback(request.client.host)):
        return None
    headers = request.headers
    if any(name in {"forwarded", "via", "x-real-ip"} or name.startswith("x-forwarded-") for name in headers):
        return None
    hosts = headers.getlist("host")
    if len(hosts) != 1 or hosts[0] not in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
        return None
    return hosts[0]


def _local_portal_request(request: Request) -> bool:
    host = local_portal_host(request)
    if host is None:
        return False
    headers = request.headers
    origins = headers.getlist("origin")
    fetch_sites = headers.getlist("sec-fetch-site")
    if origins and origins != [f"{request.scope['scheme']}://{host}"]:
        return False
    if fetch_sites and fetch_sites not in (["same-origin"], ["none"]):
        return False
    return request.method in {"GET", "HEAD"} or bool(origins) or fetch_sites == ["same-origin"]


async def local_portal_admin(request: Request) -> LocalPortalPrincipal | None:
    token = request.app.state.settings.eval_api_token
    if not token or _default_history_tenant() != "eval":
        return None
    try:
        user = await request.app.state.authenticator.authenticate(token.get_secret_value())
    except AuthError:
        return None
    if user.tenant_id == "eval" and user.role == "admin" and not user.scopes:
        return LocalPortalPrincipal.model_validate(user.model_dump())
    return None


async def _request_principal(request: Request, authorization: str | None, *, guest: bool) -> Principal:
    if authorization is None and "authorization" not in request.headers:
        if (_local_portal_request(request) and not portal_token_required(request.app.state.settings)
                and (user := await local_portal_admin(request))):
            return user
        if guest and (guest_user := await _guest_principal(request)):
            return guest_user
    return await request.app.state.authenticator.authenticate(_bearer(authorization))
