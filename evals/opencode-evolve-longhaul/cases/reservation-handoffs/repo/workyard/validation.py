"""Validation for the first-generation tool registers."""

from deskcore.errors import fail
from deskcore.fields import Row
from deskcore.planning import ordered_window, same_scope
from deskcore.storage import Store


def require_scope(store: Store, row: Row) -> None:
    store.require("depots", row["scope"])


def reservation(store: Store, row: Row) -> None:
    require_scope(store, row)
    tool = same_scope(store, "tools", row["tool"], row["scope"])
    same_scope(store, "borrowers", row["borrower"], row["scope"])
    if row["units"] > tool["units"]:
        fail("conflict", "reservation exceeds catalog quantity")
    if row["pickup"] is not None:
        store.require("depots", row["pickup"])
    if row["closed"] is not None:
        ordered_window(row["day"], row["closed"])


def service(store: Store, row: Row) -> None:
    require_scope(store, row)
    tool = same_scope(store, "tools", row["tool"], row["scope"])
    ordered_window(row["day"], row["end"])
    if row["units"] > tool["units"]:
        fail("conflict", "service allocation exceeds catalog quantity")
    if row["result_day"] is not None:
        ordered_window(row["day"], row["result_day"])


def pool_order(store: Store, row: Row) -> None:
    require_scope(store, row)
    same_scope(store, "tools", row["tool"], row["scope"])
    if row["substitute"] is not None:
        store.require("tools", row["substitute"])
    if row["closed"] is not None:
        ordered_window(row["day"], row["closed"])


def return_record(store: Store, row: Row) -> None:
    require_scope(store, row)
    same_scope(store, "tools", row["tool"], row["scope"])
    same_scope(store, "borrowers", row["borrower"], row["scope"])
    if row["damaged"] > row["units"]:
        fail("bad_request", "damaged units exceed intake")
    if row["allowed"] is not None and row["allowed"] > row["damaged"]:
        fail("bad_request", "accepted damage exceeds reported damage")
    if row["settled"] is not None:
        ordered_window(row["day"], row["settled"])
