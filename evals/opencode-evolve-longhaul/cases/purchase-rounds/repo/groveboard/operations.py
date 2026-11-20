"""Legacy order pipeline, delivery counts and paper deposit balance."""

from collections import Counter

from deskcore.application import Command, Context
from deskcore.errors import fail
from deskcore.fields import Row, date_field, id_field, optional_id
from deskcore.planning import same_scope
from deskcore.query import between, selected


def pipeline(ctx: Context, scope: str, day: str) -> list[Row]:
    ctx.store.require("markets", scope)
    rows = selected(ctx.store.rows("purchase-orders"), scope=scope, status="draft")
    groups: dict[str, list[Row]] = {}
    for row in between(rows, "day", None, day):
        groups.setdefault(row["supplier"], []).append(row)
    return [
        {
            "supplier": supplier,
            "orders": [row["id"] for row in orders],
            "units": sum(row["units"] for row in orders),
            "cents": sum(row["units"] * row["cents"] for row in orders),
        }
        for supplier, orders in sorted(groups.items())
    ]


def shortages(ctx: Context, scope: str, first: str, last: str) -> list[Row]:
    ctx.store.require("markets", scope)
    rows = between(selected(ctx.store.rows("delivery-lines"), scope=scope), "day", first, last)
    return [
        {
            "line": row["id"],
            "product": row["product"],
            "day": row["day"],
            "units": row["ordered"] - row["received"],
            "credit_due_cents": (row["ordered"] - row["received"] - (row["credit"] or 0))
            * row["cents"],
        }
        for row in rows
        if row["ordered"] > row["received"]
    ]


def deposits(ctx: Context, scope: str, day: str) -> list[Row]:
    ctx.store.require("markets", scope)
    rows = selected(ctx.store.rows("crate-moves"), scope=scope, status="posted")
    items: Counter[str] = Counter()
    cents: Counter[str] = Counter()
    for row in between(rows, "day", None, day):
        signed = row["units"] if row["kind"] == "out" else -row["units"]
        items[row["supplier"]] += signed
        cents[row["supplier"]] += signed * row["rate"]
    return [
        {"supplier": supplier, "crates": count, "deposit_cents": cents[supplier]}
        for supplier, count in sorted(items.items())
    ]


def price(ctx: Context, scope: str, day: str, supplier: str | None) -> list[Row]:
    ctx.store.require("markets", scope)
    if supplier is not None:
        same_scope(ctx.store, "suppliers", supplier, scope)
    rows = selected(ctx.store.rows("agreements"), scope=scope, supplier=supplier)
    return [
        {"supplier": row["supplier"], "id": row["id"], "cents": row["cents"]}
        for row in rows
        if row["status"] in ("active", "replaced") and row["day"] <= day <= row["end"]
    ]


def cancel(ctx: Context, id: str) -> Row:
    order = ctx.store.require("purchase-orders", id)
    if order["status"] != "draft":
        fail("conflict", "only a draft purchase order can be cancelled")
    saved = ctx.store.put("purchase-orders", {**order, "status": "cancelled"})
    ctx.audit("purchase-orders.cancelled", id, supplier=order["supplier"], market=order["scope"])
    return saved


def overview(ctx: Context, scope: str, day: str) -> Row:
    incoming = pipeline(ctx, scope, day)
    lines = between(selected(ctx.store.rows("delivery-lines"), scope=scope), "day", None, day)
    return {
        "draft_order_cents": sum(row["cents"] for row in incoming),
        "draft_order_units": sum(row["units"] for row in incoming),
        "received_units": sum(row["received"] for row in lines),
        "paper_deposits": deposits(ctx, scope, day),
    }


COMMANDS = (
    Command(
        "purchase-orders",
        "pipeline",
        "GET",
        "/v1/purchase-orders/pipeline",
        "records.read",
        "purchase-orders.pipeline",
        "List draft orders by supplier",
        (id_field("scope", "market"), date_field()),
        pipeline,
    ),
    Command(
        "purchase-orders",
        "cancel",
        "POST",
        "/v1/purchase-orders/cancel",
        "records.write",
        "purchase-orders.cancel",
        "Cancel a draft purchase order",
        (id_field(),),
        cancel,
    ),
    Command(
        "delivery-lines",
        "shortages",
        "GET",
        "/v1/delivery-lines/shortages",
        "records.read",
        "delivery-lines.shortages",
        "Find shortages in expected deliveries",
        (id_field("scope", "market"), date_field("first"), date_field("last")),
        shortages,
    ),
    Command(
        "crate-moves",
        "deposits",
        "GET",
        "/v1/crate-moves/deposits",
        "records.read",
        "crate-moves.deposits",
        "Sum paper deposits by supplier",
        (id_field("scope", "market"), date_field()),
        deposits,
    ),
    Command(
        "agreements",
        "price",
        "GET",
        "/v1/agreements/price",
        "records.read",
        "agreements.price",
        "List active prices for the day",
        (id_field("scope", "market"), date_field(), optional_id("supplier", "supplier ID")),
        price,
    ),
)
EVENTS = {"purchase-orders.cancelled"}
EN = {
    "purchase-orders.pipeline": "Order pipeline",
    "purchase-orders.cancel": "Order cancelled",
    "delivery-lines.shortages": "Delivery shortages",
    "crate-moves.deposits": "Paper deposits",
    "agreements.price": "Agreed prices",
}
FR = {
    "purchase-orders.pipeline": "Commandes à traiter",
    "purchase-orders.cancel": "Commande annulée",
    "delivery-lines.shortages": "Manques à la livraison",
    "crate-moves.deposits": "Consignes papier",
    "agreements.price": "Tarifs convenus",
}
