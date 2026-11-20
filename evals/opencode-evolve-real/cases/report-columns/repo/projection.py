from dataclasses import dataclass

COLUMNS = {
    "id": ("ID", "编号"),
    "title": ("Title", "标题"),
    "count": ("Count", "数量"),
    "note": ("Note", "备注"),
}
ALIASES = {"key": "id", "name": "title", "total": "count"}


@dataclass(frozen=True)
class Report:
    headers: tuple[str, ...]
    rows: tuple[tuple, ...]
    warnings: tuple[str, ...]


def project(rows: list[dict], columns: list[str] | None = None, locale: str = "en") -> Report:
    """Select only requested fields, validating each in row and selection order.

    id has alias key and requires a string (empty is valid). count has alias
    total and requires an actual int including negatives and zero. For either
    column, missing, null or the wrong type raises ValueError("row N: COLUMN").
    N is the one-based row number and COLUMN is the canonical name.

    title has alias name. It and note accept strings or None (render None
    as an empty string). A missing title renders an empty string and adds a
    warning "row N: missing title". A missing note renders empty without a
    warning. Invalid types raise the same ValueError as for id and count.
    Unselected columns and unknown input fields are ignored.
    """
    return Report(("ID", "Title", "Count"), tuple(
        (row.get("id", ""), row.get("title", ""), row.get("count", 0)) for row in rows
    ), ())
