"""The baseline depot inventory, queue and intake views."""

from collections import Counter

from deskcore.application import Command, Context
from deskcore.errors import fail
from deskcore.fields import Row, date_field, id_field, optional_id
from deskcore.planning import same_scope
from deskcore.query import between, selected, totals
from deskcore.storage import Store


def open_reservations(store: Store, scope: str | None = None) -> list[Row]:
    return selected(store.rows("reservations"), scope=scope, status="open")


def ready_quantity(store: Store, tool: Row) -> int:
    if tool["status"] != "ready":
        return 0
    reserved = sum(
        row["units"] for row in open_reservations(store, tool["scope"]) if row["tool"] == tool["id"]
    )
    return max(0, tool["units"] - reserved)


def service_at(store: Store, scope: str, day: str) -> list[Row]:
    return [
        row
        for row in selected(store.rows("service-logs"), scope=scope, status="planned")
        if row["day"] <= day <= row["end"]
    ]


def capacity(ctx: Context, scope: str, day: str) -> list[Row]:
    ctx.store.require("depots", scope)
    service = totals(service_at(ctx.store, scope, day), "tool", "units")
    return [
        {
            "tool": tool["id"],
            "on_hand": tool["units"],
            "reserved": tool["units"] - ready_quantity(ctx.store, tool)
            if tool["status"] == "ready"
            else 0,
            "service": service.get(tool["id"], 0),
            "available": max(0, ready_quantity(ctx.store, tool) - service.get(tool["id"], 0)),
        }
        for tool in selected(ctx.store.rows("tools"), scope=scope)
    ]


def queue(ctx: Context, scope: str, day: str, tool: str | None) -> list[Row]:
    ctx.store.require("depots", scope)
    if tool is not None:
        same_scope(ctx.store, "tools", tool, scope)
    rows = selected(open_reservations(ctx.store, scope), tool=tool)
    return sorted(
        between(rows, "day", None, day), key=lambda row: (row["day"], row["tool"], row["id"])
    )


def pickup_sheet(ctx: Context, scope: str, first: str, last: str) -> list[Row]:
    ctx.store.require("depots", scope)
    reservations = [
        row
        for row in ctx.store.rows("reservations")
        if (row["pickup"] or row["scope"]) == scope and row["status"] != "cancelled"
    ]
    rows = []
    for reservation in between(reservations, "day", first, last):
        tool = ctx.store.require("tools", reservation["tool"])
        borrower = ctx.store.require("borrowers", reservation["borrower"])
        rows.append(
            {
                "id": reservation["id"],
                "day": reservation["day"],
                "tool": tool["name"],
                "borrower": borrower["name"],
                "units": reservation["units"],
                "source": reservation["scope"],
                "status": reservation["status"],
            }
        )
    return sorted(rows, key=lambda row: (row["day"], row["id"]))


def rotate_loan(ctx: Context, id: str, day: str) -> Row:
    row = ctx.store.require("reservations", id)
    if row["status"] != "open" or day < row["day"]:
        fail("conflict", "only a due reservation can be cancelled")
    result = ctx.store.put("reservations", {**row, "closed": day, "status": "cancelled"})
    ctx.audit("reservations.cancelled", id, depot=row["scope"], day=day)
    return result


def cost_report(ctx: Context, scope: str, first: str, last: str) -> Row:
    ctx.store.require("depots", scope)
    rows = between(selected(ctx.store.rows("returns"), scope=scope), "day", first, last)
    totals_by_model = Counter()
    for row in rows:
        if row["allowed"] is not None:
            totals_by_model[row["tool"]] += (row["damaged"] - row["allowed"]) * row["rate"]
    return {
        "tool_cents": dict(sorted(totals_by_model.items())),
        "total_cents": sum(totals_by_model.values()),
        "pending": [row["id"] for row in rows if row["status"] == "logged" and row["damaged"]],
    }


def overview(ctx: Context, scope: str, day: str) -> Row:
    stock = capacity(ctx, scope, day)
    due = queue(ctx, scope, day, None)
    return {
        "on_hand_units": sum(row["on_hand"] for row in stock),
        "available_units": sum(row["available"] for row in stock),
        "due_reservations": [row["id"] for row in due],
        "service_units": sum(row["service"] for row in stock),
    }


COMMANDS = (
    Command(
        "tools",
        "capacity",
        "GET",
        "/v1/tools/capacity",
        "records.read",
        "tools.capacity",
        "Review available stock by model",
        (id_field("scope", "depot"), date_field()),
        capacity,
    ),
    Command(
        "reservations",
        "queue",
        "GET",
        "/v1/reservations/queue",
        "records.read",
        "reservations.queue",
        "List reservations due by day",
        (id_field("scope", "depot"), date_field(), optional_id("tool", "tool model")),
        queue,
    ),
    Command(
        "reservations",
        "sheet",
        "GET",
        "/v1/reservations/sheet",
        "records.read",
        "reservations.sheet",
        "Print the depot pickup sheet",
        (id_field("scope", "depot"), date_field("first"), date_field("last")),
        pickup_sheet,
    ),
    Command(
        "reservations",
        "cancel",
        "POST",
        "/v1/reservations/cancel",
        "records.write",
        "reservations.cancel",
        "Cancel a due reservation",
        (id_field(), date_field()),
        rotate_loan,
    ),
    Command(
        "returns",
        "charges",
        "GET",
        "/v1/returns/charges",
        "records.read",
        "returns.charges",
        "Review settled damage charges",
        (id_field("scope", "depot"), date_field("first"), date_field("last")),
        cost_report,
    ),
)
EVENTS = {"reservations.cancelled"}
EN = {
    "tools.capacity": "Tool availability",
    "reservations.queue": "Reservations due",
    "reservations.sheet": "Pickup sheet",
    "reservations.cancel": "Reservation cancelled",
    "returns.charges": "Return charges",
}
FR = {
    "tools.capacity": "Disponibilité des outils",
    "reservations.queue": "Réservations à servir",
    "reservations.sheet": "Liste des collectes",
    "reservations.cancel": "Réservation annulée",
    "returns.charges": "Frais de retour",
}
