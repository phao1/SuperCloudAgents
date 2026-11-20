"""Assigned follow-up work linked to the original desk registers."""

from collections.abc import Sequence
from dataclasses import dataclass

from .application import Command, Context
from .errors import fail
from .fields import (
    Field,
    Row,
    actor_key,
    date_field,
    enum_field,
    id_field,
    optional_text,
    text_field,
)
from .planning import ordered_window, require_status
from .query import selected
from .records import Entity
from .storage import Table

NAME = "followups"
TABLE = Table(NAME, 2)
EVENTS = {"followups.opened", "followups.assigned", "followups.rescheduled", "followups.closed"}
POLICY = {
    "followups.open": "operator",
    "followups.assign": "manager",
    "followups.reschedule": "operator",
    "followups.close": "operator",
    "followups.read": "reader",
}
MESSAGES = {
    "en": {
        "followups.open": "Follow-up opened",
        "followups.assign": "Follow-up assigned",
        "followups.reschedule": "Follow-up rescheduled",
        "followups.close": "Follow-up closed",
        "followups.show": "Follow-up details",
        "followups.list": "Follow-up queue",
    },
    "fr": {
        "followups.open": "Suivi ouvert",
        "followups.assign": "Suivi attribué",
        "followups.reschedule": "Suivi reprogrammé",
        "followups.close": "Suivi terminé",
        "followups.show": "Détails du suivi",
        "followups.list": "File des suivis",
    },
}


def _owner(ctx: Context, owner: str) -> str:
    member = ctx.store.member(owner)
    if member is None or member["active"] is not True or member["role"] == "reader":
        fail("bad_request", "follow-up requires an active operator or manager")
    return owner


def _can_edit(ctx: Context, row: Row) -> None:
    if ctx.principal.role != "manager" and row["owner"] != ctx.principal.actor:
        fail("forbidden", "only the assignee or a manager can change this follow-up")


def _by_due(rows: list[Row]) -> list[Row]:
    return sorted(rows, key=lambda row: (row["due"], row["priority"] != "urgent", row["id"]))


@dataclass(frozen=True)
class Followups:
    scope_table: str
    entities: Sequence[Entity]

    def source_scope(self, ctx: Context, register: str, source: str) -> str:
        if all(entity.name != register for entity in self.entities):
            fail("bad_request", "follow-up source must be a legacy register")
        row = ctx.store.require(register, source)
        return row["id"] if register == self.scope_table else row["scope"]

    def open(
        self,
        ctx: Context,
        id: str,
        register: str,
        source: str,
        owner: str,
        day: str,
        due: str,
        priority: str,
        note: str,
    ) -> Row:
        scope = self.source_scope(ctx, register, source)
        _owner(ctx, owner)
        ordered_window(day, due)
        ctx.store.require(self.scope_table, scope)
        if selected(ctx.store.rows(NAME), register=register, source=source, status="open"):
            fail("conflict", "an open follow-up already exists for this source")
        row = ctx.store.insert(
            NAME,
            {
                "id": id,
                "scope": scope,
                "register": register,
                "source": source,
                "day": day,
                "due": due,
                "priority": priority,
                "owner": owner,
                "note": note,
                "status": "open",
                "closed_day": None,
                "outcome": None,
            },
        )
        ctx.audit("followups.opened", id, register=register, source=source, owner=owner, due=due)
        return row

    def assign(self, ctx: Context, id: str, owner: str) -> Row:
        row = ctx.store.require(NAME, id)
        require_status(row, "open")
        _owner(ctx, owner)
        if owner == row["owner"]:
            return row
        saved = ctx.store.put(NAME, {**row, "owner": owner})
        ctx.audit("followups.assigned", id, previous=row["owner"], owner=owner)
        return saved

    def reschedule(self, ctx: Context, id: str, due: str, reason: str) -> Row:
        row = ctx.store.require(NAME, id)
        require_status(row, "open")
        _can_edit(ctx, row)
        ordered_window(row["day"], due)
        if due == row["due"]:
            return row
        saved = ctx.store.put(NAME, {**row, "due": due})
        ctx.audit("followups.rescheduled", id, previous=row["due"], due=due, reason=reason)
        return saved

    def close(self, ctx: Context, id: str, day: str, outcome: str) -> Row:
        row = ctx.store.require(NAME, id)
        require_status(row, "open")
        _can_edit(ctx, row)
        ordered_window(row["day"], day)
        saved = ctx.store.put(
            NAME, {**row, "status": "closed", "closed_day": day, "outcome": outcome}
        )
        ctx.audit("followups.closed", id, scope=row["scope"], day=day, outcome=outcome)
        return saved

    def show(self, ctx: Context, id: str) -> Row:
        return ctx.store.require(NAME, id)

    def list(
        self,
        ctx: Context,
        scope: str,
        status: str | None,
        owner: str | None,
        priority: str | None,
    ) -> list[Row]:
        ctx.store.require(self.scope_table, scope)
        return _by_due(
            selected(
                ctx.store.rows(NAME), scope=scope, status=status, owner=owner, priority=priority
            )
        )

    def commands(self) -> tuple[Command, ...]:
        owner = Field("owner", actor_key, "active desk member")
        due = date_field("due", "last day to respond")
        return (
            Command(
                NAME,
                "open",
                "POST",
                "/v1/followups/open",
                "followups.open",
                "followups.open",
                "Assign a task against a legacy record",
                (
                    id_field(),
                    id_field("register", "legacy table"),
                    id_field("source", "source ID"),
                    owner,
                    date_field(),
                    due,
                    enum_field("priority", "task priority", "normal", "urgent"),
                    text_field("note", "task description"),
                ),
                self.open,
            ),
            Command(
                NAME,
                "assign",
                "POST",
                "/v1/followups/assign",
                "followups.assign",
                "followups.assign",
                "Change a follow-up's assignee",
                (id_field(), owner),
                self.assign,
            ),
            Command(
                NAME,
                "reschedule",
                "POST",
                "/v1/followups/reschedule",
                "followups.reschedule",
                "followups.reschedule",
                "Change a follow-up's deadline",
                (id_field(), due, text_field("reason", "reason for deadline change")),
                self.reschedule,
            ),
            Command(
                NAME,
                "close",
                "POST",
                "/v1/followups/close",
                "followups.close",
                "followups.close",
                "Close a follow-up",
                (id_field(), date_field(), text_field("outcome", "result")),
                self.close,
            ),
            Command(
                NAME,
                "show",
                "GET",
                "/v1/followups/show",
                "followups.read",
                "followups.show",
                "Show one follow-up",
                (id_field(),),
                self.show,
            ),
            Command(
                NAME,
                "list",
                "GET",
                "/v1/followups/list",
                "followups.read",
                "followups.list",
                "Read the follow-up queue",
                (
                    id_field("scope", "desk scope"),
                    optional_text("status", "task status"),
                    optional_text("owner", "desk member"),
                    optional_text("priority", "normal or urgent"),
                ),
                self.list,
            ),
        )


def due(ctx: Context, scope: str, day: str) -> Row:
    rows = [
        row
        for row in selected(ctx.store.rows(NAME), scope=scope)
        if row["day"] <= day <= row["due"] or row["due"] < day
    ]
    active = [row for row in rows if row["closed_day"] is None or day < row["closed_day"]]
    return {
        "open": len(active),
        "overdue": [row["id"] for row in _by_due(active) if row["due"] < day],
        "due_today": [row["id"] for row in _by_due(active) if row["due"] == day],
    }
