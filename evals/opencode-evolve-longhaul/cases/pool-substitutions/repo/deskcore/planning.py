"""Validation helpers for business workflows with a desk scope and date window."""

from collections.abc import Iterable

from .errors import fail
from .fields import Row
from .query import overlaps
from .storage import Store


def same_scope(store: Store, table: str, record_id: str, scope: str) -> Row:
    row = store.require(table, record_id)
    if row["scope"] != scope:
        fail("conflict", f"{table}/{record_id} belongs to another desk")
    return row


def ordered_window(first: str, last: str) -> None:
    if first > last:
        fail("bad_request", "end precedes start")


def busy(
    rows: Iterable[Row],
    first: str,
    last: str,
    *,
    first_field: str = "day",
    last_field: str = "end",
) -> list[Row]:
    return [row for row in rows if overlaps(first, last, row[first_field], row[last_field])]


def require_status(row: Row, *statuses: str) -> None:
    if row["status"] not in statuses:
        fail("conflict", f"{row['id']} must be " + " or ".join(statuses))


def group_rows(rows: Iterable[Row], key: str) -> dict[str, list[Row]]:
    groups: dict[str, list[Row]] = {}
    for row in rows:
        if row.get(key) is not None:
            groups.setdefault(row[key], []).append(row)
    return dict(sorted(groups.items()))
