"""References, quantities and offer windows in the market's legacy registers."""

from deskcore.errors import fail
from deskcore.fields import Row
from deskcore.planning import ordered_window, same_scope
from deskcore.storage import Store


def require_scope(store: Store, row: Row) -> None:
    store.require("markets", row["scope"])


def supplier_scope(store: Store, row: Row) -> None:
    require_scope(store, row)
    same_scope(store, "suppliers", row["supplier"], row["scope"])


def product(store: Store, row: Row) -> None:
    supplier_scope(store, row)


def purchase(store: Store, row: Row) -> None:
    supplier_scope(store, row)
    sku = same_scope(store, "products", row["product"], row["scope"])
    if sku["supplier"] != row["supplier"]:
        fail("conflict", "purchase order lists another product supplier")
    if row["released"] is not None:
        ordered_window(row["day"], row["released"])


def delivery(store: Store, row: Row) -> None:
    require_scope(store, row)
    same_scope(store, "products", row["product"], row["scope"])
    if row["received"] > row["ordered"]:
        fail("bad_request", "received units exceed the delivery line")
    if row["credit"] is not None and row["credit"] > row["ordered"] - row["received"]:
        fail("bad_request", "credit exceeds the recorded shortfall")
    if row["credited"] is not None:
        ordered_window(row["day"], row["credited"])


def agreement(store: Store, row: Row) -> None:
    supplier_scope(store, row)
    ordered_window(row["day"], row["end"])
    if row["previous"] == row["id"]:
        fail("bad_request", "agreement cannot replace itself")
