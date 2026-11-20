"""Operator directory and membership commands."""

from .application import Context
from .errors import fail
from .fields import Row


def whoami(ctx: Context) -> Row:
    principal = ctx.principal
    return {"actor": principal.actor, "name": principal.name, "role": principal.role}


def members(ctx: Context) -> list[Row]:
    return ctx.store.members()


def add_member(ctx: Context, actor: str, name: str, role: str) -> Row:
    previous = ctx.store.member(actor)
    if previous is not None:
        fail("conflict", f"member already exists: {actor}")
    member = {"name": name, "role": role, "active": True}
    ctx.store.put_member(actor, member)
    ctx.audit("team.added", actor, role=role)
    return {"actor": actor, **member}


def suspend_member(ctx: Context, actor: str) -> Row:
    member = ctx.store.member(actor)
    if member is None:
        fail("not_found", f"member does not exist: {actor}")
    if actor == ctx.principal.actor:
        fail("conflict", "use a different manager to suspend this member")
    if member["active"]:
        member["active"] = False
        ctx.store.put_member(actor, member)
        ctx.audit("team.suspended", actor, role=member["role"])
    return {"actor": actor, **member}
