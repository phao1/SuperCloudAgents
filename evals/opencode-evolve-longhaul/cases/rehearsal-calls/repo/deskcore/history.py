"""Run named history upgrades in the same unit of work as command writes."""

from .application import Context
from .errors import fail
from .fields import Row, key


def upgrade(ctx: Context, name: str) -> Row:
    convert = ctx.app.upgrades.get(name)
    if convert is None or name not in ctx.app.schema:
        fail("not_found", f"unknown history upgrade: {name}")
    table = ctx.store.table(name)
    if ctx.store.upgraded(name) >= table.version:
        return {"name": name, "imported": 0}

    records = list(convert(ctx.store))
    ids = [key(row.get("id")) for row in records]
    if len(set(ids)) != len(ids):
        fail("bad_store", f"duplicate history identifiers for {name}")
    for row in records:
        previous = ctx.store.get(name, row["id"])
        if previous is not None and previous != {**row, "version": table.version}:
            fail("conflict", f"history would overwrite {name}/{row['id']}")

    imported = 0
    for row in records:
        if ctx.store.get(name, row["id"]) is None:
            ctx.store.insert(name, row)
            imported += 1
    ctx.store.record_upgrade(name, table.version)
    ctx.audit("store.upgraded", name, version=table.version, imported=imported)
    return {"name": name, "imported": imported}
