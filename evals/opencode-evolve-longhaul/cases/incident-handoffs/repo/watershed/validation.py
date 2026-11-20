"""Station references for the original field logs."""

from deskcore.fields import Row
from deskcore.planning import ordered_window, same_scope
from deskcore.storage import Store


def require_scope(store: Store, row: Row) -> None:
    store.require("stations", row["scope"])


def visit(store: Store, row: Row) -> None:
    require_scope(store, row)
    store.require("crews", row["crew"])
    if row["dispatched"] is not None:
        ordered_window(row["dispatched"], row["day"])


def calibration(store: Store, row: Row) -> None:
    require_scope(store, row)
    same_scope(store, "meters", row["meter"], row["scope"])
    if row["reviewed"] is not None:
        ordered_window(row["day"], row["reviewed"])


def incident(store: Store, row: Row) -> None:
    require_scope(store, row)
    store.require("crews", row["crew"])
    if row["former"] is not None:
        store.require("crews", row["former"])
    if row["received"] is not None:
        ordered_window(row["day"], row["received"])
    if row["due"] is not None:
        ordered_window(row["day"], row["due"])
