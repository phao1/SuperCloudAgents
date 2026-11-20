"""Read the append-only audit feed and its stable CSV export."""

import json

from .application import Context
from .fields import Row
from .query import csv_text, selected, window

COLUMNS = ("seq", "event", "actor", "record_id", "details")


def page(
    ctx: Context,
    after: int,
    limit: int,
    actor: str | None,
    event: str | None,
) -> list[Row]:
    events = selected(ctx.store.events(), actor=actor, event=event)
    return window(events, after=after, limit=limit)


def export(ctx: Context, actor: str | None, event: str | None) -> Row:
    rows = selected(ctx.store.events(), actor=actor, event=event)
    normalized = [
        {**row, "details": json.dumps(row["details"], ensure_ascii=False, sort_keys=True)}
        for row in rows
    ]
    return {"csv": csv_text(normalized, COLUMNS), "count": len(rows)}
