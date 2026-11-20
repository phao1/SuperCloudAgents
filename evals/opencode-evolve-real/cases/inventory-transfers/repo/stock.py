from collections.abc import Iterable

from changes import Delta, compact


class Stock:
    def __init__(self, initial: dict[tuple[str, str], int]):
        self._quantities = dict(initial)

    def quantity(self, bin_name: str, sku: str) -> int:
        return self._quantities.get((bin_name, sku), 0)

    def apply(self, changes: Iterable[Delta]) -> list[Delta]:
        adjustments = compact(changes)
        updated = dict(self._quantities)
        for bin_name, sku, delta in adjustments:
            after = self.quantity(bin_name, sku) + delta
            if after < 0:
                raise ValueError("insufficient stock")
            updated[bin_name, sku] = after
        self._quantities = updated
        return adjustments
