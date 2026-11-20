from collections.abc import Iterable

Delta = tuple[str, str, int]


def compact(changes: Iterable[Delta]) -> list[Delta]:
    """Sum by (bin, SKU), omit zero totals, and preserve first-touch order."""
    totals = {}
    for bin_name, sku, delta in changes:
        original, total = totals.get(sku, (bin_name, 0))
        totals[sku] = (original, total + delta)
    return [(bin_name, sku, delta) for sku, (bin_name, delta) in totals.items() if delta]
