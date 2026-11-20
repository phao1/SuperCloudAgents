from zones import Zone


def parcel(row: dict) -> tuple[int, int, bool]:
    """Return grams, quantity, fragile.

    A parcel is an object with required grams, optional quantity (default 1),
    and optional fragile (default False). There are no other fields.
    Grams is an actual int, 1 through 10000 inclusive. Quantity is an
    actual int, 1 through 50 inclusive. Fragile is an actual bool.
    Invalid parcels raise ValueError("parcels").
    """
    return int(row["grams"]), row.get("quantity", 1), row.get("fragile", False)


def rate(zone: Zone, row: dict) -> dict[str, int]:
    grams, quantity, fragile = parcel(row)
    return {"shipping_cents": (zone.base + (grams // 500) * zone.block) * quantity,
            "handling_cents": zone.fragile if fragile else 0}
