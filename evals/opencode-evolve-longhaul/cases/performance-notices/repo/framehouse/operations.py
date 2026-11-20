"""Calendar, workload and daily box-office views."""

from deskcore.application import Command, Context
from deskcore.errors import fail
from deskcore.fields import Row, date_field, id_field, optional_id, text_field
from deskcore.planning import busy, ordered_window, same_scope
from deskcore.query import between, selected, totals
from deskcore.storage import Store


def booking_conflicts(
    store: Store,
    scope: str,
    first: str,
    last: str,
    *,
    ignore: tuple[str, ...] = (),
) -> list[Row]:
    confirmed = selected(store.rows("bookings"), scope=scope, status="confirmed")
    return [row for row in busy(confirmed, first, last) if row["id"] not in ignore]


def calendar(ctx: Context, scope: str, first: str, last: str) -> list[Row]:
    ctx.store.require("venues", scope)
    ordered_window(first, last)
    rows = booking_conflicts(ctx.store, scope, first, last)
    return sorted(rows, key=lambda row: (row["day"], row["end"], row["id"]))


def workload(ctx: Context, scope: str, first: str, last: str, person: str | None) -> list[Row]:
    ctx.store.require("venues", scope)
    if person is not None:
        same_scope(ctx.store, "crew", person, scope)
    rows = selected(ctx.store.rows("attendance"), scope=scope, person=person)
    minutes = totals(between(rows, "day", first, last), "person", "minutes")
    return [
        {"person": member, "minutes": amount, "name": ctx.store.require("crew", member)["name"]}
        for member, amount in minutes.items()
    ]


def audience(ctx: Context, scope: str, day: str) -> list[Row]:
    ctx.store.require("venues", scope)
    rows = [
        row
        for row in selected(ctx.store.rows("performances"), scope=scope, day=day)
        if row["status"] in {"scheduled", "moved"}
    ]
    return [
        {
            "performance": row["id"],
            "production": row["production"],
            "audience": sorted(row["audience"]),
        }
        for row in rows
    ]


def cancel(ctx: Context, id: str, reason: str) -> Row:
    row = ctx.store.require("bookings", id)
    if row["status"] != "confirmed":
        fail("conflict", "booking has already been cancelled")
    saved = ctx.store.put("bookings", {**row, "status": "cancelled"})
    ctx.audit("bookings.cancelled", id, scope=row["scope"], reason=reason)
    return saved


def roster(ctx: Context, scope: str, day: str) -> list[Row]:
    ctx.store.require("venues", scope)
    calls = selected(ctx.store.rows("calls"), scope=scope, day=day, status="announced")
    by_person = {}
    for row in calls:
        for person in row["crew"]:
            by_person.setdefault(person, set()).add(row["production"])
    return [
        {
            "person": person,
            "duty": ctx.store.require("crew", person)["duty"],
            "productions": sorted(productions),
        }
        for person, productions in sorted(by_person.items())
    ]


def overview(ctx: Context, scope: str, day: str) -> Row:
    marks = selected(ctx.store.rows("attendance"), scope=scope, day=day)
    reads = audience(ctx, scope, day)
    return {
        "performances_today": [item["performance"] for item in reads],
        "worked_minutes": sum(row["minutes"] for row in marks),
        "booked_blocks": len(booking_conflicts(ctx.store, scope, day, day)),
        "called_crew": [row["person"] for row in roster(ctx, scope, day)],
    }


COMMANDS = (
    Command(
        "bookings",
        "calendar",
        "GET",
        "/v1/bookings/calendar",
        "records.read",
        "bookings.calendar",
        "List confirmed bookings touching an interval",
        (id_field("scope", "venue"), date_field("first"), date_field("last")),
        calendar,
    ),
    Command(
        "crew",
        "workload",
        "GET",
        "/v1/crew/workload",
        "records.read",
        "crew.workload",
        "Sum worked minutes by crew member",
        (
            id_field("scope", "venue"),
            date_field("first"),
            date_field("last"),
            optional_id("person", "crew ID"),
        ),
        workload,
    ),
    Command(
        "performances",
        "audience",
        "GET",
        "/v1/performances/audience",
        "records.read",
        "performances.audience",
        "Read audience lists for a venue and day",
        (id_field("scope", "venue"), date_field()),
        audience,
    ),
    Command(
        "bookings",
        "cancel",
        "POST",
        "/v1/bookings/cancel",
        "records.write",
        "bookings.cancel",
        "Cancel a venue booking",
        (id_field(), text_field("reason", "cancellation reason")),
        cancel,
    ),
    Command(
        "calls",
        "roster",
        "GET",
        "/v1/calls/roster",
        "records.read",
        "calls.roster",
        "List crew on paper calls",
        (id_field("scope", "venue"), date_field()),
        roster,
    ),
)
EVENTS = {"bookings.cancelled"}
EN = {
    "bookings.calendar": "Venue calendar",
    "crew.workload": "Crew workload",
    "performances.audience": "Audience lists",
    "bookings.cancel": "Booking cancelled",
    "calls.roster": "Paper call roster",
}
FR = {
    "bookings.calendar": "Calendrier de la salle",
    "crew.workload": "Heures de l’équipe",
    "performances.audience": "Listes du public",
    "bookings.cancel": "Réservation annulée",
    "calls.roster": "Équipe convoquée",
}
