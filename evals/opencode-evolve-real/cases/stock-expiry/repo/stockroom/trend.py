"""Roll up the movement log by item, warehouse, and calendar bucket."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from .errors import StockroomError
from .models import Movement, Trend

PERIODS = ("day", "week", "month")


def bucket(day: str, grouping: str) -> str:
    if grouping == "day":
        return day
    if grouping == "month":
        return day[:7] + "-01"
    if grouping == "week":
        point = date.fromisoformat(day)
        return (point - timedelta(days=point.weekday())).isoformat()
    raise StockroomError("invalid trend period")


@dataclass
class Tally:
    received: int = 0
    shipped: int = 0
    adjusted: int = 0
    transferred: int = 0
    events: int = 0

    def add(self, movement: Movement) -> None:
        if movement.kind == "receive":
            self.received += movement.delta
        elif movement.kind == "ship":
            self.shipped -= movement.delta
        elif movement.kind == "adjust":
            self.adjusted += movement.delta
        else:
            self.transferred += movement.delta
        self.events += 1

    def result(self, period: str, warehouse: str, sku: str) -> Trend:
        net = self.received - self.shipped + self.adjusted + self.transferred
        return Trend(period, sku, warehouse, self.received, self.shipped,
                     self.adjusted, self.transferred, net, self.events)


def rollup(movements: Iterable[Movement], grouping: str) -> list[Trend]:
    if grouping not in PERIODS:
        raise StockroomError("invalid trend period")
    groups: dict[tuple[str, str, str], Tally] = {}
    for movement in movements:
        key = bucket(movement.day, grouping), movement.warehouse, movement.sku
        if key not in groups:
            groups[key] = Tally()
        groups[key].add(movement)
    return [groups[key].result(*key) for key in sorted(groups)]
