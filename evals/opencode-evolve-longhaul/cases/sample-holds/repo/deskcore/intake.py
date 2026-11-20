"""Preview, review and apply a delivered portable bundle."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from hashlib import sha256

from . import exchange
from .application import Command, Context
from .errors import fail
from .fields import Row, id_field, json_field, optional_text, text_field
from .query import selected
from .records import Entity
from .storage import Store, Table, json_bytes

NAME = "intake"
TABLE = Table(NAME, 2)
EVENTS = {"intake.prepared", "intake.applied", "intake.cancelled"}
POLICY = {
    "intake.read": "reader",
    "intake.prepare": "operator",
    "intake.apply": "manager",
    "intake.cancel": "manager",
}
MESSAGES = {
    "en": {
        "intake.prepare": "Intake ready for review",
        "intake.apply": "Reviewed intake applied",
        "intake.cancel": "Intake cancelled",
        "intake.show": "Intake details",
        "intake.list": "Bundle intakes",
    },
    "fr": {
        "intake.prepare": "Import prêt pour examen",
        "intake.apply": "Import validé appliqué",
        "intake.cancel": "Import annulé",
        "intake.show": "Détails de l’import",
        "intake.list": "Imports d’archives",
    },
}


def _entries(bundle: Row) -> list[tuple[str, str]]:
    return sorted((table, item["id"]) for table, rows in bundle["tables"].items() for item in rows)


def _fingerprint(store: Store, bundle: Row) -> str:
    snapshot = [
        {"table": table, "id": record_id, "row": store.get(table, record_id)}
        for table, record_id in _entries(bundle)
    ]
    return sha256(json_bytes(snapshot)).hexdigest()


def _summary(bundle: Row, proposal: Row) -> Row:
    result = {}
    for table, rows in sorted(bundle["tables"].items()):
        count = proposal["tables"][table]
        result[table] = {"add": count, "unchanged": len(rows) - count}
    return result


def _visible(row: Row) -> Row:
    return {key: value for key, value in row.items() if key not in {"bundle", "fingerprint"}}


@dataclass(frozen=True)
class Intake:
    entities: Sequence[Entity]

    def prepare(self, ctx: Context, id: str, source: str, bundle: Row) -> Row:
        shadow = ctx.store.preview()
        proposal = exchange.import_rows(replace(ctx, store=shadow), self.entities, bundle)
        if not _entries(bundle):
            fail("bad_request", "intake requires at least one portable record")
        saved = ctx.store.insert(
            NAME,
            {
                "id": id,
                "source": source,
                "status": "ready",
                "prepared_by": ctx.principal.actor,
                "reviewed_by": None,
                "reason": None,
                "bundle": bundle,
                "fingerprint": _fingerprint(ctx.store, bundle),
                "tables": _summary(bundle, proposal),
                "inserted": None,
            },
        )
        ctx.audit("intake.prepared", id, source=source, tables=sorted(proposal["tables"]))
        return _visible(saved)

    def apply(self, ctx: Context, id: str) -> Row:
        row = ctx.store.require(NAME, id)
        if row["status"] == "applied":
            return _visible(row)
        if row["status"] != "ready":
            fail("conflict", "only ready intake can be applied")
        if _fingerprint(ctx.store, row["bundle"]) != row["fingerprint"]:
            fail("conflict", "records changed since intake was prepared")
        result = exchange.import_rows(ctx, self.entities, row["bundle"])
        saved = ctx.store.put(
            NAME,
            {
                **row,
                "status": "applied",
                "reviewed_by": ctx.principal.actor,
                "inserted": result["count"],
            },
        )
        ctx.audit("intake.applied", id, source=row["source"], inserted=result["count"])
        return _visible(saved)

    def cancel(self, ctx: Context, id: str, reason: str) -> Row:
        row = ctx.store.require(NAME, id)
        if row["status"] != "ready":
            fail("conflict", "only ready intake can be cancelled")
        saved = ctx.store.put(
            NAME,
            {
                **row,
                "status": "cancelled",
                "reason": reason,
                "reviewed_by": ctx.principal.actor,
            },
        )
        ctx.audit("intake.cancelled", id, source=row["source"], reason=reason)
        return _visible(saved)

    def show(self, ctx: Context, id: str) -> Row:
        return _visible(ctx.store.require(NAME, id))

    def list(self, ctx: Context, status: str | None, source: str | None) -> list[Row]:
        return [
            _visible(row) for row in selected(ctx.store.rows(NAME), status=status, source=source)
        ]

    def commands(self) -> tuple[Command, ...]:
        return (
            Command(
                NAME,
                "prepare",
                "POST",
                "/v1/intake/prepare",
                "intake.prepare",
                "intake.prepare",
                "Preview a version-one bundle before importing",
                (
                    id_field(),
                    text_field("source", "sending desk"),
                    json_field("bundle", "portable bundle object"),
                ),
                self.prepare,
            ),
            Command(
                NAME,
                "apply",
                "POST",
                "/v1/intake/apply",
                "intake.apply",
                "intake.apply",
                "Confirm the reviewed intake",
                (id_field(),),
                self.apply,
            ),
            Command(
                NAME,
                "cancel",
                "POST",
                "/v1/intake/cancel",
                "intake.cancel",
                "intake.cancel",
                "Cancel a pending intake",
                (id_field(), text_field("reason", "cancellation note")),
                self.cancel,
            ),
            Command(
                NAME,
                "show",
                "GET",
                "/v1/intake/show",
                "intake.read",
                "intake.show",
                "Read a recorded intake summary",
                (id_field(),),
                self.show,
            ),
            Command(
                NAME,
                "list",
                "GET",
                "/v1/intake/list",
                "intake.read",
                "intake.list",
                "Browse intake summaries",
                (optional_text("status", "intake status"), optional_text("source", "sending desk")),
                self.list,
            ),
        )
