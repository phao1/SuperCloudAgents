"""Versioned ZIP layout with checksums over uncompressed members."""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, BinaryIO, Literal
from uuid import UUID
from zipfile import ZipFile

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import Float

from sca.history.catalog import ARTIFACTS, CHAMPIONS, TABLES, TASKS, TENANTS, HistoryTable

BLOCK_SIZE = 1024 * 1024
Row = dict[str, Any]


class HistoryError(Exception):
    pass


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    size: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class MissingObject(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: UUID
    name: str


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    format_version: Literal[1] = 1
    schema_revision: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_]{1,32}$")]
    created_at: AwareDatetime
    counts: dict[str, Annotated[int, Field(ge=0)]]
    files: dict[str, Member]
    missing_objects: list[MissingObject] = Field(default_factory=list)


def object_member(entry: HistoryTable, row: Row) -> str | None:
    if entry is ARTIFACTS:
        return f"objects/artifacts/{row['id']}"
    if entry is TASKS and row["events_archived_at"] is not None:
        return f"objects/events/{row['id']}"
    return None


def _json_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    raise TypeError("Invalid history value")


def json_line(row: Row) -> bytes:
    return json.dumps(row, default=_json_value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8") + b"\n"


def decode_row(entry: HistoryTable, line: bytes) -> Row:
    def typed(name: str, value: Any) -> Any:
        column = entry.table.c[name]
        if value is None and column.nullable:
            return None
        if isinstance(column.type, JSONB):
            if isinstance(value, (dict, list)):
                return value
            raise ValueError
        kind = column.type.python_type
        if kind is UUID and isinstance(value, str):
            return UUID(value)
        if kind is datetime and isinstance(value, str):
            timestamp = datetime.fromisoformat(value)
            if timestamp.utcoffset() is not None:
                return timestamp
            raise ValueError
        if isinstance(column.type, Float) and type(value) in (float, int):
            return float(value)
        if kind is Decimal and isinstance(value, str):
            amount = Decimal(value)
            if amount.is_finite():
                return amount
            raise ValueError
        if type(value) is not kind:
            raise ValueError
        return value

    try:
        row = json.loads(line)
        if not isinstance(row, dict):
            raise TypeError
        # History bundles written before backend-aware transfer omitted this field.
        if entry is CHAMPIONS and row.setdefault("agent_backend", "opencode") not in ("opencode", "dsh"):
            raise ValueError
        if set(row) != set(entry.columns.split()):
            raise ValueError
        return {name: typed(name, value) for name, value in row.items()}
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise HistoryError(f"Invalid record in {entry.member}") from exc


class DigestWriter:
    def __init__(self, file: BinaryIO) -> None:
        self.file = file
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        self.size += len(data)
        return self.file.write(data)

    def member(self) -> Member:
        return Member(size=self.size, sha256=self.digest.hexdigest())


class BundleWriter:
    def __init__(self, zip_file: ZipFile) -> None:
        self.zip = zip_file
        self.files: dict[str, Member] = {}

    @contextmanager
    def open(self, name: str) -> Iterator[DigestWriter]:
        with self.zip.open(name, "w", force_zip64=True) as output:
            writer = DigestWriter(output)
            yield writer
        self.files[name] = writer.member()


def check_digest(file: BinaryIO, member: Member) -> None:
    digest = hashlib.sha256()
    size = 0
    while data := file.read(BLOCK_SIZE):
        size += len(data)
        digest.update(data)
    if size != member.size or digest.hexdigest() != member.sha256:
        raise HistoryError("History object checksum mismatch")


class BundleReader:
    def __init__(self, zip_file: ZipFile) -> None:
        self.zip = zip_file
        names = self.zip.namelist()
        if len(set(names)) != len(names) or "manifest.json" not in names:
            raise HistoryError("History ZIP has a missing manifest or duplicate files")
        try:
            self.manifest = Manifest.model_validate_json(self.zip.read("manifest.json"))
        except ValidationError as exc:
            raise HistoryError("Invalid history manifest") from exc
        if set(names) != self.manifest.files.keys() | {"manifest.json"}:
            raise HistoryError("ZIP members differ from the history manifest")
        counts = self.manifest.counts
        if counts.keys() - {"missing_objects"} != {entry.name for entry in TABLES} | {"objects"}:
            raise HistoryError("Unexpected history table counts")
        self._missing = {(item.task_id, item.name) for item in self.manifest.missing_objects}
        if len(self._missing) != len(self.manifest.missing_objects) or len(self._missing) != counts.get(
            "missing_objects", 0,
        ):
            raise HistoryError("Invalid missing object count or duplicate references")

    def verify(self) -> None:
        tables = {entry.member for entry in TABLES}
        objects = set(self.manifest.files) - tables
        if not tables <= self.manifest.files.keys() or len(objects) != self.manifest.counts["objects"]:
            raise HistoryError("History ZIP has missing table files or an invalid object count")
        unreferenced = objects.copy()
        missing = self._missing.copy()
        tenants: set[str] = set()
        for entry in TABLES:
            with self.zip.open(entry.member) as file:
                self._verify_table(entry, file, tenants, unreferenced, missing)
        if unreferenced or missing:
            raise HistoryError("History ZIP contains unreferenced objects")
        for name in objects:
            with self.zip.open(name) as file:
                check_digest(file, self.manifest.files[name])

    def _verify_table(
        self, entry: HistoryTable, file: BinaryIO, tenants: set[str], unreferenced: set[str],
        missing: set[tuple[UUID, str]],
    ) -> None:
        digest = hashlib.sha256()
        size = count = 0
        for line in file:
            digest.update(line)
            size += len(line)
            count += 1
            row = decode_row(entry, line)
            if entry is TENANTS:
                tenants.add(row["id"])
            elif row["tenant_id"] not in tenants:
                raise HistoryError(f"Unknown tenant in {entry.member}")
            name = object_member(entry, row)
            if name is None:
                continue
            if entry is ARTIFACTS and (key := (row["task_id"], row["name"])) in self._missing:
                if key not in missing or name in self.manifest.files:
                    raise HistoryError("Missing artifact is also present or referenced twice")
                missing.remove(key)
            else:
                if name not in unreferenced:
                    raise HistoryError("History object is missing or referenced twice")
                unreferenced.remove(name)
        if (Member(size=size, sha256=digest.hexdigest()) != self.manifest.files[entry.member]
                or count != self.manifest.counts[entry.name]):
            raise HistoryError(f"Invalid checksum or record count for {entry.member}")

    def batches(self, entry: HistoryTable, limit: int = 100) -> Iterator[list[Row]]:
        with self.zip.open(entry.member) as file:
            rows = []
            for line in file:
                rows.append(decode_row(entry, line))
                if len(rows) == limit:
                    yield rows
                    rows = []
            if rows:
                yield rows
