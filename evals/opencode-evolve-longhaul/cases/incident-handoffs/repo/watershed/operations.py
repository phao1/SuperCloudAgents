"""Baseline station itinerary, instrument and physical sample views."""

from deskcore.application import Command, Context
from deskcore.errors import fail
from deskcore.fields import Row, date_field, id_field, optional_id, text_field
from deskcore.planning import same_scope
from deskcore.query import between, histogram, selected


def itinerary(ctx: Context, crew: str, day: str) -> list[Row]:
    ctx.store.require("crews", crew)
    rows = selected(ctx.store.rows("visits"), crew=crew, day=day)
    return sorted(
        (row for row in rows if row["status"] != "cancelled"),
        key=lambda row: (row["position"], row["scope"], row["id"]),
    )


def tolerances(ctx: Context, scope: str, day: str, meter: str | None) -> list[Row]:
    ctx.store.require("stations", scope)
    if meter is not None:
        same_scope(ctx.store, "meters", meter, scope)
    rows = selected(ctx.store.rows("calibration-logs"), scope=scope, meter=meter)
    latest: dict[str, Row] = {}
    for row in sorted(between(rows, "day", None, day), key=lambda item: (item["day"], item["id"])):
        if row["status"] != "void":
            latest[row["meter"]] = row
    result = []
    for meter_id, row in sorted(latest.items()):
        limit = ctx.store.require("meters", meter_id)["tolerance"]
        result.append(
            {
                "meter": meter_id,
                "day": row["day"],
                "reading": row["reading"],
                "tolerance": limit,
                "outside_limit": abs(row["reading"]) > limit,
            }
        )
    return result


def urgent(ctx: Context, scope: str, day: str) -> list[Row]:
    ctx.store.require("stations", scope)
    rows = between(
        selected(ctx.store.rows("incidents"), scope=scope, priority="urgent"), "day", None, day
    )
    return [row for row in rows if row["status"] != "closed"]


def sample_register(ctx: Context, scope: str, first: str, last: str, water: str | None) -> Row:
    ctx.store.require("stations", scope)
    rows = between(
        selected(ctx.store.rows("samples"), scope=scope, water=water), "day", first, last
    )
    return {
        "counts": histogram(rows, "status"),
        "rows": [
            {"id": row["id"], "day": row["day"], "water": row["water"], "status": row["status"]}
            for row in sorted(rows, key=lambda item: (item["day"], item["id"]))
        ],
    }


def close_incident(ctx: Context, id: str, reason: str) -> Row:
    row = ctx.store.require("incidents", id)
    if row["status"] == "closed":
        fail("conflict", "incident has already been closed")
    saved = ctx.store.put("incidents", {**row, "status": "closed"})
    ctx.audit("incidents.closed", id, station=row["scope"], reason=reason)
    return saved


def overview(ctx: Context, scope: str, day: str) -> Row:
    samples = between(selected(ctx.store.rows("samples"), scope=scope), "day", None, day)
    visits = between(selected(ctx.store.rows("visits"), scope=scope), "day", None, day)
    return {
        "sample_counts": histogram(samples, "status"),
        "open_urgent_incidents": [row["id"] for row in urgent(ctx, scope, day)],
        "visit_counts": histogram(visits, "status"),
        "meter_alerts": [
            row["meter"] for row in tolerances(ctx, scope, day, None) if row["outside_limit"]
        ],
    }


COMMANDS = (
    Command(
        "visits",
        "itinerary",
        "GET",
        "/v1/visits/itinerary",
        "records.read",
        "visits.itinerary",
        "Get the crew's field itinerary",
        (id_field("crew", "field crew"), date_field()),
        itinerary,
    ),
    Command(
        "meters",
        "tolerances",
        "GET",
        "/v1/meters/tolerances",
        "records.read",
        "meters.tolerances",
        "Find the latest meter deviations",
        (id_field("scope", "station"), date_field(), optional_id("meter", "meter ID")),
        tolerances,
    ),
    Command(
        "incidents",
        "urgent",
        "GET",
        "/v1/incidents/urgent",
        "records.read",
        "incidents.urgent",
        "List urgent incidents still open",
        (id_field("scope", "station"), date_field()),
        urgent,
    ),
    Command(
        "incidents",
        "close",
        "POST",
        "/v1/incidents/close",
        "records.write",
        "incidents.close",
        "Close a station incident",
        (id_field(), text_field("reason", "closure note")),
        close_incident,
    ),
    Command(
        "samples",
        "register",
        "GET",
        "/v1/samples/register",
        "records.read",
        "samples.register",
        "List sample counts and dispositions",
        (
            id_field("scope", "station"),
            date_field("first"),
            date_field("last"),
            optional_id("water", "sample type"),
        ),
        sample_register,
    ),
)
EVENTS = {"incidents.closed"}
EN = {
    "visits.itinerary": "Crew itinerary",
    "meters.tolerances": "Meter deviations",
    "incidents.urgent": "Urgent incidents",
    "incidents.close": "Incident closed",
    "samples.register": "Sample register",
}
FR = {
    "visits.itinerary": "Itinéraire de l’équipe",
    "meters.tolerances": "Écarts des instruments",
    "incidents.urgent": "Incidents urgents",
    "incidents.close": "Incident clos",
    "samples.register": "Registre des échantillons",
}
