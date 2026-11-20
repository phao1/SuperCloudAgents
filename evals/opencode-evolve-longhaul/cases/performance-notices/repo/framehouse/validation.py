"""References and date rules for the venue's paper registers."""

from deskcore.errors import fail
from deskcore.fields import Row
from deskcore.planning import ordered_window, same_scope
from deskcore.storage import Store


def require_scope(store: Store, row: Row) -> None:
    store.require("venues", row["scope"])


def production(store: Store, row: Row) -> None:
    venue = store.require("venues", row["scope"])
    if row["seats"] > venue["capacity"]:
        fail("conflict", "production exceeds home venue capacity")


def fits(store: Store, scope: str, production_id: str) -> None:
    venue = store.require("venues", scope)
    show = store.require("productions", production_id)
    if show["seats"] > venue["capacity"]:
        fail("conflict", "production exceeds venue capacity")


def call(store: Store, row: Row) -> None:
    fits(store, row["scope"], row["production"])
    for crew in row["crew"]:
        same_scope(store, "crew", crew, row["scope"])
    if row["announced"] is not None:
        ordered_window(row["announced"], row["day"])


def performance(store: Store, row: Row) -> None:
    fits(store, row["scope"], row["production"])
    if row["prior_scope"] is not None:
        store.require("venues", row["prior_scope"])
    if row["sent"] is not None:
        ordered_window(row["sent"], row["day"])


def booking(store: Store, row: Row) -> None:
    fits(store, row["scope"], row["production"])
    ordered_window(row["day"], row["end"])
    if row["former_scope"] is not None:
        store.require("venues", row["former_scope"])


def attendance(store: Store, row: Row) -> None:
    fits(store, row["scope"], row["production"])
    same_scope(store, "crew", row["person"], row["scope"])
    if row["original"] is not None and row["original"] > 1440:
        fail("bad_request", "an attendance day has at most 1440 minutes")
    if row["reviewed"] is not None:
        ordered_window(row["day"], row["reviewed"])
