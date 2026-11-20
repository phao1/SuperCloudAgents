"""Immutable value objects crossing the inventory boundaries."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Warehouse:
    code: str
    name: str
    active: bool = True


@dataclass(frozen=True)
class Supplier:
    code: str
    name: str
    contact: str = ""
    active: bool = True


@dataclass(frozen=True)
class Item:
    sku: str
    name: str
    category: str
    unit: str
    active: bool = True


@dataclass(frozen=True)
class Lot:
    id: int
    sku: str
    warehouse: str
    code: str
    quantity: int
    received: str
    supplier: str | None


@dataclass(frozen=True)
class Intake:
    sku: str
    warehouse: str
    code: str
    quantity: int
    received: str
    supplier: str | None = None
    memo: str = ""


@dataclass(frozen=True)
class Movement:
    id: int
    sku: str
    warehouse: str
    lot: str
    day: str
    kind: str
    delta: int
    counterpart: str | None
    memo: str


@dataclass(frozen=True)
class Note:
    id: int
    lot_id: int
    day: str
    text: str
    archived: bool


@dataclass(frozen=True)
class Balance:
    sku: str
    warehouse: str
    quantity: int
    positive_lots: int


@dataclass(frozen=True)
class Aging:
    sku: str
    warehouse: str
    quantity: int
    newest_days: int
    oldest_days: int


@dataclass(frozen=True)
class AuditLine:
    lot_id: int
    sku: str
    warehouse: str
    code: str
    quantity: int
    recorded: int
    difference: int



@dataclass(frozen=True)
class Stocktake:
    id: int
    warehouse: str
    label: str
    opened: str
    closed: str | None
    status: str


@dataclass(frozen=True)
class Observation:
    lot_id: int
    sku: str
    lot: str
    expected: int
    observed: int | None

    @property
    def difference(self) -> int | None:
        return None if self.observed is None else self.observed - self.expected


@dataclass(frozen=True)
class Trend:
    period: str
    sku: str
    warehouse: str
    received: int
    shipped: int
    adjusted: int
    transferred: int
    net: int
    events: int
