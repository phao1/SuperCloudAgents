"""Single-file desk store shared by the CLI, API and exchange tools."""

import json
import os
from collections.abc import Iterable, Mapping
from copy import copy, deepcopy
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .errors import DeskError, fail
from .fields import Row, actor_key, key, reject_constant


@dataclass(frozen=True)
class Table:
    name: str
    version: int = 1

    def __post_init__(self) -> None:
        key(self.name)
        if self.version not in (1, 2):
            raise ValueError("table version must be 1 or 2")


def json_bytes(data: Any, *, pretty: bool = False) -> bytes:
    extras = {"indent": 2} if pretty else {"separators": (",", ":")}
    return (
        json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False, **extras) + "\n"
    ).encode("utf-8")


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(exist_ok=True, parents=True)
    with NamedTemporaryFile(dir=path.parent, prefix="." + path.name + ".", delete=False) as out:
        temporary = Path(out.name)
        try:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
            out.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def fresh_state() -> Row:
    return {
        "format": 1,
        "members": {
            "lead": {"name": "Desk lead", "role": "manager", "active": True},
            "clerk": {"name": "Desk clerk", "role": "operator", "active": True},
            "viewer": {"name": "Read-only visitor", "role": "reader", "active": True},
        },
        "tables": {},
        "audit": [],
    }


class Store:
    def __init__(self, path: Path, schema: Mapping[str, Table]) -> None:
        self.path = path
        self.schema = schema
        self._data = self._load()
        self.changed = False

    def _load(self) -> Row:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return fresh_state()
        try:
            data = json.loads(content, parse_constant=reject_constant)
            self._validate(data)
        except (ValueError, TypeError, DeskError, RecursionError):
            fail("bad_store", "unreadable workspace file")
        return data

    @staticmethod
    def _validate(data: Row) -> None:
        if not isinstance(data, dict) or type(data.get("format")) is not int or data["format"] != 1:
            fail("bad_store", "unsupported workspace format")
        if not isinstance(data.get("tables"), dict) or not isinstance(data.get("members"), dict):
            fail("bad_store", "missing workspace tables or members")
        if not isinstance(data.get("audit"), list):
            fail("bad_store", "missing workspace audit trail")
        for name, bucket in data["tables"].items():
            Store._validate_records(name, bucket, version=1)
        extensions = data.get("extensions", {})
        if not isinstance(extensions, dict):
            fail("bad_store", "invalid extension table index")
        for name, bucket in extensions.items():
            if not isinstance(bucket, dict) or type(bucket.get("schema")) is not int:
                fail("bad_store", f"invalid extension envelope: {name}")
            if bucket["schema"] != 2:
                fail("bad_store", f"unsupported extension schema: {name}")
            Store._validate_records(name, bucket.get("records"), version=2)
        upgrades = data.get("upgrades", {})
        if not isinstance(upgrades, dict) or any(
            type(version) is not int or version < 1 for version in upgrades.values()
        ):
            fail("bad_store", "invalid upgrade receipts")
        for seq, row in enumerate(data["audit"], 1):
            if not isinstance(row, dict) or row.get("seq") != seq:
                fail("bad_store", "invalid audit sequence")

    @staticmethod
    def _validate_records(name: str, records: Any, *, version: int) -> None:
        if not isinstance(records, dict):
            fail("bad_store", f"invalid table: {name}")
        for record_id, row in records.items():
            if (
                not isinstance(row, dict)
                or row.get("id") != record_id
                or type(row.get("version")) is not int
                or row["version"] != version
            ):
                fail("bad_store", f"invalid record envelope: {name}/{record_id}")

    def table(self, name: str) -> Table:
        table = self.schema.get(name)
        if table is None:
            fail("not_found", f"unknown table: {name}")
        return table

    def _records(self, table: Table, *, create: bool = False) -> dict[str, Row]:
        if table.version == 1:
            return (
                self._data["tables"].setdefault(table.name, {})
                if create
                else self._data["tables"].get(table.name, {})
            )
        if create:
            extensions = self._data.setdefault("extensions", {})
            return extensions.setdefault(table.name, {"schema": 2, "records": {}})["records"]
        return self._data.get("extensions", {}).get(table.name, {}).get("records", {})

    def keys(self, name: str) -> list[str]:
        return sorted(self._records(self.table(name)))

    def rows(self, name: str) -> list[Row]:
        records = self._records(self.table(name))
        return [deepcopy(records[record_id]) for record_id in sorted(records)]

    def get(self, name: str, record_id: str) -> Row | None:
        return deepcopy(self._records(self.table(name)).get(record_id))

    def require(self, name: str, record_id: str) -> Row:
        row = self.get(name, record_id)
        if row is None:
            fail("not_found", f"unknown {name} record: {record_id}")
        return row

    def put(self, name: str, record: Row) -> Row:
        table = self.table(name)
        record_id = key(record.get("id"))
        value = deepcopy(record)
        value["version"] = table.version
        records = self._records(table, create=True)
        if records.get(record_id) != value:
            records[record_id] = value
            self.changed = True
        return deepcopy(value)

    def insert(self, name: str, record: Row) -> Row:
        if self.get(name, key(record.get("id"))) is not None:
            fail("conflict", f"{name} record already exists: {record['id']}")
        return self.put(name, record)

    def member(self, actor: str) -> Row | None:
        return deepcopy(self._data["members"].get(actor))

    def members(self) -> list[Row]:
        return [
            {"actor": actor, **deepcopy(row)}
            for actor, row in sorted(self._data["members"].items())
        ]

    def put_member(self, actor: str, member: Row) -> None:
        actor_key(actor)
        if self._data["members"].get(actor) != member:
            self._data["members"][actor] = deepcopy(member)
            self.changed = True

    def events(self) -> list[Row]:
        return deepcopy(self._data["audit"])

    def append_event(self, actor: str, event: str, record_id: str, details: Row) -> Row:
        audit = self._data["audit"]
        row = {
            "seq": len(audit) + 1,
            "version": 1,
            "event": event,
            "actor": actor,
            "record_id": record_id,
            "details": deepcopy(details),
        }
        audit.append(row)
        self.changed = True
        return deepcopy(row)

    def upgraded(self, name: str) -> int:
        return self._data.get("upgrades", {}).get(name, 0)

    def record_upgrade(self, name: str, version: int) -> None:
        self._data.setdefault("upgrades", {})[name] = version
        self.changed = True

    def describe(self) -> Row:
        tables = [
            {"name": table.name, "version": table.version, "count": len(self._records(table))}
            for table in sorted(self.schema.values(), key=lambda value: value.name)
        ]
        return {"format": 1, "tables": tables, "upgrades": dict(self._data.get("upgrades", {}))}

    def export(self, names: Iterable[str]) -> dict[str, list[Row]]:
        return {name: self.rows(name) for name in sorted(names)}

    def preview(self) -> "Store":
        shadow = copy(self)
        shadow._data = deepcopy(self._data)
        shadow.changed = False
        return shadow

    def flush(self) -> None:
        if self.changed:
            try:
                write_atomic(self.path, json_bytes(self._data, pretty=True))
            except OSError:
                fail("io_error", "could not save workspace")
            self.changed = False
