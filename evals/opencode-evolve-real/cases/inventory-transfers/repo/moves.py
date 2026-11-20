from dataclasses import dataclass

from stock import Stock


@dataclass(frozen=True)
class Move:
    source: str
    destination: str
    sku: str
    quantity: int


def apply(stock: Stock, moves: list[Move]) -> list[tuple[str, str, int]]:
    def stream():
        for move in moves:
            yield move.source, move.sku, -move.quantity
            yield move.destination, move.sku, move.quantity
    return stock.apply(stream())
