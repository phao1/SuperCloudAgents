"""Export a read-only snapshot and import it without scheduling work."""

import asyncio
import gzip
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Literal
from zipfile import ZIP_DEFLATED, ZipFile

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util import CommandError
from sqlalchemy import Column, MetaData, Table, Text, cast, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import UUID, insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sca.archive import HISTORY_EVENT_ARCHIVE, archive_key
from sca.core.errors import NotFoundError
from sca.core.models import HISTORY_MISSING_ARTIFACT_URI, TERMINAL_STATES, TaskState
from sca.db import schema as db
from sca.history.bundle import (
    BundleReader,
    BundleWriter,
    HistoryError,
    Manifest,
    MissingObject,
    Row,
    json_line,
    object_member,
)
from sca.history.catalog import (
    ARTIFACTS,
    CANDIDATES,
    CHAMPIONS,
    EVAL_RUNS,
    EVOLUTION_EVENTS,
    EVOLUTIONS,
    PROJECTS,
    TABLES,
    TASKS,
    TENANTS,
    HistoryTable,
    Snapshot,
)
from sca.history.objects import Storage, copy_artifact, object_uri, read_object, write_object
from sca.history.privacy import clean_text, clean_value
from sca.storage.local import history_artifact_key, key_for

_FIRST_REVISION = "0024"
_IMPORT_LOCK = 0x53434148495354
STOP_REASON = "imported while running"
IMPORTED = "history_imported"
_TRACKED_PARENTS = {TASKS, EVAL_RUNS}
_INSERT_ONCE = {TENANTS, PROJECTS, EVOLUTIONS, CANDIDATES, CHAMPIONS, *_TRACKED_PARENTS}
_TERMINAL_RUNS = {"completed", "failed", "stopped", "budget_exhausted", "patience_exhausted"}
_OWNERS = Table(
    "sca_history_owners", MetaData(),
    Column("table_name", Text, primary_key=True),
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("previous_state", Text),
    schema="pg_temp",
)

type ExportProgress = Callable[[Literal["rows", "objects"]], None]


def require_ancestor(current: str, source: str) -> None:
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "db" / "migrations"))
    try:
        for revision in (source, current):
            script = scripts.get_revision(revision)
            if script is None or script.revision != revision:
                raise HistoryError("Unknown history schema revision")
        list(scripts.iterate_revisions(current, source))
    except (RevisionError, CommandError) as exc:
        raise HistoryError("The database schema must include the history archive's revision") from exc


async def _revision(conn: AsyncConnection, database: str) -> str:
    # A restricted API role must fail instead of silently exporting a partial RLS view.
    await conn.exec_driver_sql("SET LOCAL row_security = off")
    heads = await conn.run_sync(lambda sync: MigrationContext.configure(sync).get_current_heads())
    if len(heads) == 1:
        with suppress(HistoryError):
            require_ancestor(heads[0], _FIRST_REVISION)
            return heads[0]
    version = "、".join(clean_text(head) for head in heads) or "未迁移"
    raise HistoryError(f"{database}版本 {version}，需先升级到 {_FIRST_REVISION}")


def _export_row(row: Row) -> Row:
    return {key: clean_value(value) if key not in {"id", "tenant_id"} else value
            for key, value in row.items()}


async def _write_tables(
    conn: AsyncConnection, snapshot: Snapshot, bundle: BundleWriter, progress: ExportProgress | None,
) -> dict[str, int]:
    counts = {}
    for entry in TABLES:
        count = 0
        with bundle.open(entry.member) as file:
            async with conn.stream(snapshot.rows(entry).execution_options(yield_per=200)) as result:
                async for row in result.mappings():
                    file.write(json_line(_export_row(dict(row))))
                    count += 1
                    if progress:
                        progress("rows")
        counts[entry.name] = count
    return counts


def _copy_archive(store: Storage, key: str, task_id: str, output: BinaryIO) -> None:
    with (
        read_object(store, key) as (source, _),
        gzip.GzipFile(fileobj=source) as original,
        gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as target,
    ):
        for line in original:
            event = json.loads(line)
            if event["task_id"] != task_id:
                raise HistoryError("Archived event belongs to another task")
            target.write(json_line(clean_value(event)))


def _export_object(entry: HistoryTable, row: Row, bundle: BundleWriter, store: Storage) -> None:
    member = object_member(entry, row)
    assert member is not None
    if entry is TASKS:
        with bundle.open(member) as output:
            _copy_archive(store, archive_key(row["id"], result=row["result"]), str(row["id"]), output)
        return
    if row["uri"] == HISTORY_MISSING_ARTIFACT_URI:
        raise NotFoundError("History artifact was missing at the source")
    key = history_artifact_key if row["imported_task"] is True else key_for
    copy_artifact(store, key(row["task_id"], row["name"]), row["name"],
                  row["content_type"], row["size_bytes"], bundle, member)


async def _write_objects(
    conn: AsyncConnection, snapshot: Snapshot, bundle: BundleWriter, store: Storage,
    *, strict: bool, progress: ExportProgress | None,
) -> tuple[int, list[MissingObject]]:
    count = 0
    missing = []
    for entry in (ARTIFACTS, TASKS):
        query = snapshot.rows(entry, artifact_objects=True)
        if entry is TASKS:
            query = query.where(db.tasks.c.events_archived_at.is_not(None))
        async with conn.stream(query.execution_options(yield_per=200)) as result:
            async for row in result.mappings():
                try:
                    await asyncio.to_thread(_export_object, entry, row, bundle, store)
                except NotFoundError as exc:
                    if entry is TASKS:
                        raise HistoryError("任务的事件归档文件缺失。") from exc
                    if strict:
                        raise HistoryError("严格模式下发现缺失的制品文件。") from exc
                    missing.append(MissingObject(task_id=row["task_id"], name=clean_text(row["name"])))
                else:
                    count += 1
                if progress:
                    progress("objects")
    return count, missing


async def export_history(
    engine: AsyncEngine, store: Storage, output: Path, *,
    tenants: tuple[str, ...] = (), since: datetime | None = None,
    strict: bool = False, progress: ExportProgress | None = None,
) -> Manifest:
    fd, path = tempfile.mkstemp(prefix=".sca-history-", suffix=".zip", dir=output.parent)
    try:
        with os.fdopen(fd, "w+b") as file, ZipFile(file, "w", ZIP_DEFLATED, compresslevel=3) as zip_file:
            bundle = BundleWriter(zip_file)
            async with engine.connect() as conn:
                await conn.execution_options(isolation_level="REPEATABLE READ")
                async with conn.begin():
                    await conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    revision = await _revision(conn, "源库")
                    created_at = await conn.scalar(select(func.clock_timestamp()))
                    snapshot = Snapshot(tenants, since, champion_backend=revision != _FIRST_REVISION)
                    counts = await _write_tables(conn, snapshot, bundle, progress)
                    counts["objects"], missing = await _write_objects(
                        conn, snapshot, bundle, store, strict=strict, progress=progress,
                    )
                    counts["missing_objects"] = len(missing)
            manifest = Manifest(
                schema_revision=revision, created_at=created_at, counts=counts, files=bundle.files,
                missing_objects=missing,
            )
            zip_file.writestr("manifest.json", manifest.model_dump_json())
        os.replace(path, output)
        return manifest
    finally:
        Path(path).unlink(missing_ok=True)


def _prepare(entry: HistoryTable, row: Row, snapshot_at: datetime, quota: int) -> None:
    if entry is TENANTS:
        row["max_active_tasks"] = quota
    elif entry is PROJECTS:
        row["settings"] = {
            **row["settings"], IMPORTED: True,
            "history_source_default": row["settings"].get("history_source_default", row["is_default"]),
        }
        row["is_default"] = False
    elif entry is CHAMPIONS:
        row["metrics"] = {
            **row["metrics"], IMPORTED: True,
            "history_source_active": row["metrics"].get("history_source_active", row["active"]),
        }
        row["active"] = False
    elif entry is TASKS:
        row.update(result={**row["result"], IMPORTED: True}, slot_held=False, slot_requested_at=None)
        if row["events_archived_at"] is not None:
            row["result"][HISTORY_EVENT_ARCHIVE] = True
        else:
            row["result"].pop(HISTORY_EVENT_ARCHIVE, None)
        if row["state"] not in TERMINAL_STATES:
            if row["events_archived_at"] is not None:
                raise HistoryError("An unfinished task has archived events")
            stopped_at = max(snapshot_at, row["updated_at"])
            row.update(state=TaskState.CANCELLED.value, state_reason=STOP_REASON,
                       updated_at=stopped_at, finished_at=stopped_at)
    elif entry in (EVAL_RUNS, EVOLUTIONS):
        field = "variant" if entry is EVAL_RUNS else "config"
        row[field] = {**row[field], IMPORTED: True}
        if row["status"] not in _TERMINAL_RUNS:
            row[field]["reason"] = STOP_REASON
            stopped_at = max(snapshot_at, row.get("updated_at", row["created_at"]))
            row.update(status="stopped", finished_at=stopped_at)
            if entry is EVOLUTIONS:
                row["updated_at"] = stopped_at


class Importer:
    def __init__(
        self, conn: AsyncConnection, bundle: BundleReader, store: Storage, quota: int,
        *, champion_backend: bool,
    ) -> None:
        self.conn = conn
        self.bundle = bundle
        self.store = store
        self.quota = quota
        self.champion_backend = champion_backend
        self.written: list[str] = []

    async def run(self) -> dict[str, int]:
        await self.conn.exec_driver_sql("""
            CREATE TEMP TABLE sca_history_owners (
                table_name text NOT NULL, id uuid NOT NULL, previous_state text,
                PRIMARY KEY (table_name, id)
            ) ON COMMIT DROP
        """)
        counts = dict.fromkeys((entry.name for entry in TABLES), 0)
        for entry in TABLES:
            for batch in self.bundle.batches(entry):
                counts[entry.name] += await self._insert(entry, batch)
        await self._finish_tasks()
        counts["objects"] = len(self.written)
        return counts

    async def _parents(self, entry: HistoryTable, rows: list[Row]) -> list[Row]:
        for key, parent, new_only in entry.parents:
            ids = {row[key] for row in rows if row[key] is not None}
            if not ids:
                continue
            query = select(parent.c.id, parent.c.tenant_id).where(parent.c.id.in_(ids))
            if new_only:
                if parent is db.evolutions:
                    # A later archive can contain a new champion whose candidate arrived later.
                    imported = parent.c.config[IMPORTED].astext == "true"
                else:
                    owned = _OWNERS.c
                    query = query.outerjoin(_OWNERS, (owned.table_name == parent.name)
                                             & (owned.id == parent.c.id))
                    imported = owned.id.is_not(None)
                query = query.add_columns(imported.label("allow_children"))
            found = {row.id: row for row in (await self.conn.execute(query))}
            for row in rows:
                if row[key] is not None and (
                    row[key] not in found or found[row[key]].tenant_id != row["tenant_id"]
                ):
                    raise HistoryError(f"Invalid parent tenant or ID in {entry.member}")
            if new_only:
                rows = [row for row in rows if row[key] is None or found[row[key]].allow_children]
        return rows

    async def _insert(self, entry: HistoryTable, rows: list[Row]) -> int:
        rows = await self._parents(entry, rows)
        if not rows:
            return 0
        if entry is EVOLUTION_EVENTS:
            return await self._audit_events(rows)
        previous = {row["id"]: row["state"] for row in rows if entry is TASKS
                    and row["state"] not in TERMINAL_STATES}
        for row in rows:
            _prepare(entry, row, self.bundle.manifest.created_at, self.quota)
            if entry is ARTIFACTS:
                member = object_member(entry, row)
                assert member is not None
                if (blob := self.bundle.manifest.files.get(member)) is None:
                    row["uri"] = HISTORY_MISSING_ARTIFACT_URI
                else:
                    key = history_artifact_key(row["task_id"], row["name"])
                    row.update(uri=object_uri(self.store, key), size_bytes=blob.size)
            elif entry is CHAMPIONS and not self.champion_backend:
                backend = row.pop("agent_backend")
                if backend != "opencode":
                    raise HistoryError("目标库版本 0024，需先升级到 0025")
        table = entry.table
        stmt = insert(table).values(rows)
        if entry in _INSERT_ONCE:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(table.primary_key))
        if entry in _TRACKED_PARENTS:
            inserted = set((await self.conn.scalars(stmt.returning(table.c.id))).all())
            if inserted:
                await self.conn.execute(insert(_OWNERS), [
                    {"table_name": entry.name, "id": row_id, "previous_state": previous.get(row_id)}
                    for row_id in inserted
                ])
            if entry is TASKS:
                await self._publish(entry, [row for row in rows if row["id"] in inserted])
            return len(inserted)
        result = await self.conn.execute(stmt)
        if entry is ARTIFACTS:
            await self._publish(entry, rows)
        return result.rowcount

    async def _audit_events(self, rows: list[Row]) -> int:
        # Serial IDs are local to each database. Source IDs and event times deduplicate audit rows.
        keys = []
        for row in rows:
            source = row["payload"].setdefault("history_source_id", row.pop("id"))
            keys.append((row["tenant_id"], row["created_at"], str(source)))
        event = db.evolution_events.c
        source = func.coalesce(event.payload["history_source_id"].astext, cast(event.id, Text))
        key = tuple_(event.tenant_id, event.created_at, source)
        known = set(await self.conn.execute(select(*key.clauses).where(key.in_(keys))))
        new = [row for row, value in zip(rows, keys, strict=True) if value not in known]
        if not new:
            return 0
        return (await self.conn.execute(insert(db.evolution_events).values(new))).rowcount

    async def _publish(self, entry: HistoryTable, rows: list[Row]) -> None:
        for row in rows:
            member = object_member(entry, row)
            if member is None or row.get("uri") == HISTORY_MISSING_ARTIFACT_URI:
                continue
            if entry is ARTIFACTS:
                key = history_artifact_key(row["task_id"], row["name"])
                media_type = row["content_type"]
            else:
                key = archive_key(row["id"], result=row["result"])
                media_type = "application/gzip"
            created = await asyncio.to_thread(
                write_object, self.store, key, self.bundle.zip, member,
                self.bundle.manifest.files[member], media_type,
            )
            if created:
                self.written.append(key)

    async def _finish_tasks(self) -> None:
        await self.conn.execute(text("""
            INSERT INTO events (tenant_id, task_id, seq, type, payload, created_at)
            SELECT t.tenant_id, t.id, coalesce(max(e.seq), 0) + 1, 'task.state_changed',
                   jsonb_build_object('from', o.previous_state, 'to', 'CANCELLED',
                                      'reason', CAST(:reason AS text)), t.updated_at
            FROM pg_temp.sca_history_owners AS o
            JOIN tasks AS t ON t.id = o.id
            LEFT JOIN events AS e ON e.task_id = t.id
            WHERE o.table_name = 'tasks' AND o.previous_state IS NOT NULL
            GROUP BY t.id, o.previous_state
        """), {"reason": STOP_REASON})


async def import_history(
    engine: AsyncEngine, store: Storage, source: Path, *, default_quota: int = 20,
) -> dict[str, int]:
    with ZipFile(source) as zip_file:
        bundle = BundleReader(zip_file)
        async with engine.begin() as conn:
            if not await conn.scalar(select(func.pg_try_advisory_xact_lock(_IMPORT_LOCK))):
                raise HistoryError("Another history import is running on this database")
            revision = await _revision(conn, "目标库")
            require_ancestor(revision, bundle.manifest.schema_revision)
            await asyncio.to_thread(bundle.verify)
            importer = Importer(conn, bundle, store, default_quota,
                                champion_backend=revision != _FIRST_REVISION)
            try:
                counts = await importer.run()
            except BaseException:
                # Keep objects if the transaction's commit itself fails with an unknown outcome.
                for key in reversed(importer.written):
                    with suppress(Exception):
                        await store.delete(key)
                raise
        return counts
