"""Desk roles apply to both local terminal operators and embedded API clients."""

from collections.abc import Mapping
from dataclasses import dataclass

from .errors import fail
from .fields import Row

LEVELS = {"reader": 10, "operator": 20, "manager": 30}


@dataclass(frozen=True)
class Principal:
    actor: str
    name: str
    role: str


def authorize(actor: str, member: Row | None, action: str, policy: Mapping[str, str]) -> Principal:
    if member is None or member.get("active") is not True:
        fail("forbidden", "desk membership is inactive or missing")
    role = member.get("role")
    if role not in LEVELS or action not in policy:
        fail("forbidden", "action is unavailable for this desk member")
    if LEVELS[role] < LEVELS[policy[action]]:
        fail("forbidden", "desk role does not allow this action")
    return Principal(actor, member["name"], role)
