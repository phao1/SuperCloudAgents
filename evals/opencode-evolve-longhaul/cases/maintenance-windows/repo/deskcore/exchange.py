"""Portable version-one export and merge, independent of extension features."""

from collections.abc import Iterable

from .application import Context
from .errors import fail
from .fields import Row, key
from .records import Entity, without_version


def export(ctx: Context, entities: Iterable[Entity], table: str | None) -> Row:
    names = tuple(entity.name for entity in entities)
    if table is not None:
        if table not in names:
            fail("not_found", f"unknown legacy table: {table}")
        names = (table,)
    return {"format": 1, "tables": ctx.store.export(names)}


def import_rows(ctx: Context, entities: Iterable[Entity], bundle: Row) -> Row:
    tables = bundle.get("tables")
    if (
        bundle.get("format") != 1
        or not isinstance(tables, dict)
        or bundle.keys() != {"format", "tables"}
    ):
        fail("bad_request", "expected a version-one portable bundle")
    known = {entity.name: entity for entity in entities}
    unknown = tables.keys() - known.keys()
    if unknown:
        fail("bad_request", f"unknown legacy table: {min(unknown)}")

    counts = {}
    for name, entity in known.items():
        if name not in tables:
            continue
        batch = tables[name]
        if not isinstance(batch, list):
            fail("bad_request", f"expected an array for {name}")
        seen: set[str] = set()
        counts[name] = 0
        for record in batch:
            if (
                not isinstance(record, dict)
                or type(record.get("version")) is not int
                or record["version"] != 1
            ):
                fail("bad_request", f"invalid record version for {name}")
            record_id = key(record.get("id"))
            if record_id in seen:
                fail("bad_request", f"duplicate bundle record: {name}/{record_id}")
            seen.add(record_id)
            data = entity.normalize(ctx.store, without_version(record))
            before = ctx.store.get(name, record_id)
            if before is None:
                ctx.store.insert(name, data)
                counts[name] += 1
            elif before != {**data, "version": 1}:
                fail("conflict", f"workspace differs from bundle: {name}/{record_id}")

    inserted = sum(counts.values())
    if inserted:
        ctx.audit("bundle.imported", "bundle", count=inserted, tables=counts)
    return {"count": inserted, "tables": counts}
