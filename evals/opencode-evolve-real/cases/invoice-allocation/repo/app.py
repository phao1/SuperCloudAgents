from dataclasses import dataclass


@dataclass(frozen=True)
class Line:
    code: str
    weight: int
    cap: int


def allocate(amount: int, lines: list[Line]) -> dict[str, int]:
    total = sum(line.weight for line in lines)
    return {line.code: min(line.cap, round(amount * line.weight / total)) if total else 0
            for line in lines}
