"""Shared desk report and date-bounded register exports."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .application import Command, Context
from .errors import fail
from .fields import Field, Row, date_field, day, id_field, optional_id
from .query import between, csv_text, histogram, selected
from .records import Entity

type Projection = Callable[[Context, str, str], Row]


@dataclass(frozen=True)
class Overview:
    scope_table: str
    entities: Sequence[Entity]
    projection: Projection

    def report(self, ctx: Context, scope: str, day: str) -> Row:
        location = ctx.store.require(self.scope_table, scope)
        activity = {}
        for entity in self.entities:
            fields = {field.name for field in entity.fields}
            if {"scope", "day", "status"} <= fields:
                history = selected(ctx.store.rows(entity.name), scope=scope)
                activity[entity.name] = histogram(between(history, "day", None, day), "status")
        return {
            "scope": scope,
            "title": location["name"],
            "day": day,
            "activity": activity,
            **self.projection(ctx, scope, day),
        }

    def export(
        self,
        ctx: Context,
        table: str,
        scope: str | None,
        first: str | None,
        last: str | None,
    ) -> Row:
        entity = next((item for item in self.entities if item.name == table), None)
        if entity is None:
            fail("not_found", f"unknown export table: {table}")
        fields = [field.name for field in entity.fields]
        if (first is not None or last is not None) and "day" not in fields:
            fail("bad_request", "this register has no day field")
        if scope is not None:
            ctx.store.require(self.scope_table, scope)
            if "scope" not in fields and table != self.scope_table:
                fail("bad_request", "this register has no scope field")
        rows = ctx.store.rows(table)
        rows = selected(rows, **({"id": scope} if table == self.scope_table else {"scope": scope}))
        if first is not None or last is not None:
            rows = between(rows, "day", first, last)
        columns = ("id", "version", *fields)
        return {"table": table, "count": len(rows), "csv": csv_text(rows, columns)}

    def commands(self) -> tuple[Command, ...]:
        dates = (
            Field("first", day, "first day to include", default=None),
            Field("last", day, "last day to include", default=None),
        )
        return (
            Command(
                "desk",
                "report",
                "GET",
                "/v1/desk/report",
                "desk.report",
                "desk.report",
                "Review one desk on a given day",
                (id_field("scope", "desk scope"), date_field()),
                self.report,
            ),
            Command(
                "desk",
                "export",
                "GET",
                "/v1/desk/export",
                "desk.export",
                "desk.export",
                "Export one legacy register as CSV",
                (
                    id_field("table", "legacy table name"),
                    optional_id("scope", "desk scope"),
                    *dates,
                ),
                self.export,
            ),
        )
