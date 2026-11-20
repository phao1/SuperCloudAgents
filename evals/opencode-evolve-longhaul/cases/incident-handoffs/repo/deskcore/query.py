"""Helpers for selecting and exporting small desk ledgers."""

import csv
import io
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date

from .errors import fail
from .fields import Row


def selected(rows: Iterable[Row], **filters: str | None) -> list[Row]:
    return [
        row
        for row in rows
        if all(value is None or row.get(key) == value for key, value in filters.items())
    ]


def between(rows: Iterable[Row], field: str, first: str | None, last: str | None) -> list[Row]:
    if first and last and first > last:
        fail("bad_request", "date range starts after it ends")
    return [
        row
        for row in rows
        if (first is None or row[field] >= first) and (last is None or row[field] <= last)
    ]


def duration(first: str, last: str) -> int:
    return (date.fromisoformat(last) - date.fromisoformat(first)).days


def overlaps(first: str, last: str, other_first: str, other_last: str) -> bool:
    return first <= other_last and other_first <= last


def histogram(rows: Iterable[Row], key: str) -> dict[str, int]:
    return dict(sorted(Counter(row[key] for row in rows).items()))


def csv_text(rows: Iterable[Row], columns: Sequence[str]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def totals(rows: Iterable[Row], key: str, amount: str) -> dict[str, int]:
    result: Counter[str] = Counter()
    for row in rows:
        result[row[key]] += row[amount]
    return dict(sorted(result.items()))


def window(rows: list[Row], *, after: int, limit: int) -> list[Row]:
    return [row for row in rows if row["seq"] > after][:limit]
