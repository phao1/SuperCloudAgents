from moves import Move, apply
from stock import Stock


def issue(stock: Stock, moves: list[Move]) -> str:
    lines = apply(stock, moves)
    if not lines:
        raise RuntimeError("no inventory changes")
    return "".join(f"{bin_name}:{sku}:{delta:+d}\n" for bin_name, sku, delta in lines)
