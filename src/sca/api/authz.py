from collections.abc import Callable, Coroutine
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request

from sca.core.models import Principal

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
}
SCOPE_ACTIONS = {"ci:report": {"ci-result"}}


class ForbiddenError(Exception):
    pass


async def principal(request: Request, authorization: str | None = Header(None)) -> Principal:
    token = None
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            token = value
    return await request.app.state.authenticator.authenticate(token)


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
        return user

    return check
