from pricing import rate
from zones import find


def quote(postcode: str, parcels: list[dict]) -> dict:
    zone = find(postcode)
    lines = [rate(zone, line) for line in parcels]
    total = sum(line["shipping_cents"] + line["handling_cents"] for line in lines)
    return {"zone": zone.name, "lines": lines, "discount_cents": 0, "total_cents": total}
