import copy
import gzip
import hashlib
import io
import json
import sys
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from zipfile import ZipFile

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from sqlalchemy.sql.elements import BindParameter, Label, TextClause

from sca.archive import archive_key, read_events
from sca.core.errors import NotFoundError
from sca.core.models import HISTORY_MISSING_ARTIFACT_URI, Artifact, TaskState
from sca.history import __main__ as history_cli
from sca.history import transfer
from sca.history.__main__ import _since
from sca.history.bundle import BundleReader, BundleWriter, HistoryError, json_line
from sca.history.catalog import TABLES
from sca.history.objects import copy_artifact, read_object, write_object
from sca.history.privacy import clean_text, clean_value, copy_text
from sca.history.transfer import STOP_REASON, export_history, import_history, require_ancestor
from sca.storage.local import LocalObjectStore, history_artifact_key, key_for
from sca.storage.s3 import S3ObjectStore

NOW = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)


class MemoryDatabase:
    """Minimal async Core adapter for tests without access to Postgres sockets."""

    def __init__(self, rows=None, *, locked=False, revision="0024"):
        self.rows = defaultdict(list, rows or {})
        self.locked = locked
        self.revision = revision
        self.commands = []
        self.owners = set()

    @asynccontextmanager
    async def connect(self):
        yield self

    @asynccontextmanager
    async def begin(self):
        previous = copy.deepcopy(self.rows)
        self.owners.clear()
        try:
            yield self
        except BaseException:
            self.rows = previous
            raise

    async def execution_options(self, **options):
        assert options == {"isolation_level": "REPEATABLE READ"}

    async def exec_driver_sql(self, statement):
        self.commands.append(statement.lstrip())

    async def run_sync(self, _):
        return (self.revision,) if self.revision else ()

    async def dispose(self):
        pass

    async def scalar(self, statement):
        if "pg_try_advisory_xact_lock" in str(statement):
            return not self.locked
        assert "clock_timestamp" in str(statement)
        return NOW

    @asynccontextmanager
    async def stream(self, statement):
        names = list(statement.selected_columns.keys())
        literals = {name: col.element.value for name, col in statement.selected_columns.items()
                    if isinstance(col, Label) and isinstance(col.element, BindParameter)}
        table = next(iter(statement.selected_columns)).table.name
        table = "tenants" if table == "history_tenants" else table
        assert statement.get_execution_options()["yield_per"] == 200
        only_archived = "tasks.events_archived_at IS NOT NULL" in str(statement)

        async def mappings():
            for row in self.rows[table]:
                if not only_archived or row["events_archived_at"] is not None:
                    yield {name: literals.get(name, row.get(name)) for name in names}

        yield SimpleNamespace(mappings=mappings)

    def _insert(self, statement):
        table = statement.table.name
        inserted = []
        for row in statement._multi_values[0]:
            key = tuple(row.get(column.name) for column in statement.table.primary_key)
            if all(item is not None for item in key) and any(
                tuple(existing.get(column.name) for column in statement.table.primary_key) == key
                for existing in self.rows[table]
            ):
                assert statement._post_values_clause is not None
                continue
            self.rows[table].append(copy.deepcopy(row))
            inserted.append(row.get("id"))
        return inserted

    async def scalars(self, statement):
        return SimpleNamespace(all=lambda: self._insert(statement))

    async def execute(self, statement, values=None):
        if isinstance(statement, TextClause):
            assert "task.state_changed" in statement.text and values == {"reason": STOP_REASON}
            return
        if statement.is_select:
            table = next(iter(statement.selected_columns)).table.name
            if table == "evolution_events":
                return [(row["tenant_id"], row["created_at"], str(row["payload"]["history_source_id"]))
                        for row in self.rows[table]]
            return [
                SimpleNamespace(
                    id=row["id"], tenant_id=row["tenant_id"],
                    allow_children=(table, row["id"]) in self.owners
                    or row.get("config", {}).get("history_imported", False),
                )
                for row in self.rows[table]
            ]
        if statement.table.name == "sca_history_owners":
            self.owners.update((row["table_name"], row["id"]) for row in values)
            return
        return SimpleNamespace(rowcount=len(self._insert(statement)))


@pytest.fixture
async def exported(tmp_path):
    task, archived, artifact, evolution, run = (uuid4() for _ in range(5))
    candidate, champion = uuid4(), uuid4()
    objects = LocalObjectStore(tmp_path / "origin")
    patch = b"diff --git a/test b/test\n+example\n"
    await objects.put(key_for(task, "patch.diff"), patch)
    await objects.put(archive_key(archived), gzip.compress(json_line({
        "task_id": archived, "seq": 1, "type": "agent.turn_started", "created_at": NOW,
        "payload": {"turn": 1, "virtualKey": "fixture-only-gateway-value"},
    })))
    base_task = {"tenant_id": "eval", "kind": "coding", "created_at": NOW, "updated_at": NOW,
                 "project_id": None, "state_reason": None, "usage": {}, "finished_at": None}
    db = MemoryDatabase({
        "tenants": [{"id": "eval", "display_name": "Eval", "created_at": NOW, "status": "active",
                     "deleted_at": None, "membership_required": False, "plan": "fixture-only-plan"}],
        "tasks": [
            {**base_task, "id": task, "state": "PAUSED", "events_archived_at": None,
             "spec": {"instruction": "历史", "env": {"TOKEN": "fixture-only-task-value"}},
             "result": {"patch_artifact": "patch.diff"}, "billing_usage": {"cost": 42}},
            {**base_task, "id": archived, "state": "SUCCEEDED", "events_archived_at": NOW,
             "spec": {}, "result": {}},
        ],
        "artifacts": [
            {"id": artifact, "tenant_id": "eval", "task_id": task, "name": "patch.diff",
             "content_type": "text/x-diff", "size_bytes": len(patch), "created_at": NOW},
        ],
        "eval_runs": [
            {"id": run, "tenant_id": "eval", "suite": "fake", "suite_hash": "suite-sha",
             "status": "queued", "variant": {"api_tokens": ["fixture-only-eval-value"]},
             "created_by": "test", "created_at": NOW, "finished_at": None},
        ],
        "evolutions": [
            {"id": evolution, "tenant_id": "eval", "status": "awaiting_approval",
             "config": {"suite": "fake"}, "created_at": NOW, "updated_at": NOW, "created_by": "test",
             "best_champion_id": None, "spent_usd": "0.001", "finished_at": None},
        ],
        "evolution_candidates": [
            {"id": candidate, "tenant_id": "eval", "evolution_id": evolution, "generation": 1,
             "overlay": {}, "overlay_sha256": "overlay-sha", "hypothesis": "fixture",
             "diagnosis": {}, "static_check": {}, "dev_run_id": run, "decision": "promoted",
             "created_at": NOW},
        ],
        "champions": [
            {"id": champion, "tenant_id": "eval", "agent_version": "fake.1", "model": "fake",
             "overlay": {}, "overlay_sha256": "overlay-sha", "candidate_id": candidate,
             "metrics": {}, "active": True, "created_at": NOW},
        ],
        "evolution_events": [
            {"id": 7, "tenant_id": "eval", "type": "rollout.updated", "actor": "fixture",
             "payload": {"champion_id": str(champion)}, "created_at": NOW},
        ],
        "api_tokens": [{"token_hash": "fixture-only-token-hash"}],
        "billing_invoices": [{"id": "fixture-only-invoice"}],
    })
    path = tmp_path / "history.zip"
    manifest = await export_history(db, objects, path)
    return path, manifest, task, archived, patch, db


async def test_transfer_with_fake_db_and_real_local_objects(exported, tmp_path):
    path, manifest, task, archived, patch, source = exported
    assert source.commands == ["SET TRANSACTION READ ONLY", "SET LOCAL row_security = off"]
    assert manifest.schema_revision == "0024"
    assert manifest.counts["tasks"] == 2 and manifest.counts["objects"] == 2
    with ZipFile(path) as bundle:
        assert set(bundle.namelist()) == {"manifest.json", *manifest.files}
        assert all(b"fixture-only-" not in bundle.read(entry.member) for entry in TABLES)

    target = MemoryDatabase()
    objects = LocalObjectStore(tmp_path / "destination")
    counts = await import_history(target, objects, path, default_quota=26)
    assert counts["objects"] == 2 and counts["tasks"] == 2
    live_task, terminal_task = target.rows["tasks"]
    assert live_task["id"] == task
    assert live_task["state"] == TaskState.CANCELLED and live_task["state_reason"] == STOP_REASON
    assert live_task["slot_held"] is False and live_task["slot_requested_at"] is None
    assert live_task["spec"] == {"instruction": "历史"}
    assert terminal_task["state"] == TaskState.SUCCEEDED
    assert all(row["result"]["history_imported"] for row in target.rows["tasks"])
    assert target.rows["tenants"][0]["max_active_tasks"] == 26
    assert "plan" not in target.rows["tenants"][0]
    for table, field in (("evolutions", "config"), ("eval_runs", "variant")):
        row, = target.rows[table]
        assert row["status"] == "stopped" and row["finished_at"] == NOW
        assert row[field]["reason"] == STOP_REASON
    key = history_artifact_key(task, "patch.diff")
    assert target.rows["artifacts"][0]["uri"] == objects._path(key).as_uri()
    assert await objects.get(key) == patch
    assert (await read_events(objects, archived, result=terminal_task["result"]))[0].payload == {"turn": 1}
    imported_champion, = target.rows["champions"]
    assert not imported_champion["active"]
    assert imported_champion["metrics"]["history_source_active"]
    assert target.rows["evolution_events"][0]["payload"]["history_source_id"] == 7
    assert not any((await import_history(target, objects, path)).values())


async def test_runtime_switch_shares_storage_without_exposing_original_events(exported, tmp_path):
    path, manifest, _, archived, _, _ = exported
    objects = LocalObjectStore(tmp_path / "origin")
    target = MemoryDatabase()
    original = await objects.get(archive_key(archived))
    assert (await import_history(target, objects, path))["objects"] == 2
    result = next(row["result"] for row in target.rows["tasks"] if row["id"] == archived)

    assert await objects.get(archive_key(archived)) == original
    assert (await read_events(objects, archived, result=result))[0].payload == {"turn": 1}
    assert not any((await import_history(target, objects, path)).values())
    again = await export_history(target, objects, tmp_path / "again.zip")
    members = [name for name in manifest.files if name.startswith("objects/events/")]
    assert len(members) == 1 and again.files[members[0]] == manifest.files[members[0]]

    # An older import uses the original key. A missing new import cannot fall back to it.
    assert await read_events(objects, archived, result={"history_imported": True})
    await objects.delete(archive_key(archived, result=result))
    with pytest.raises(NotFoundError):
        await read_events(objects, archived, result=result)


async def test_missing_artifact_round_trip_and_reexport(exported, tmp_path):
    path, _, task, _, patch, source = exported
    objects = LocalObjectStore(tmp_path / "origin")
    await objects.delete(key_for(task, "patch.diff"))
    manifest = await export_history(source, objects, path)
    assert manifest.counts["objects"] == manifest.counts["missing_objects"] == 1
    assert manifest.counts["artifacts"] == 1
    assert [obj.model_dump() for obj in manifest.missing_objects] == [{"task_id": task, "name": "patch.diff"}]
    with ZipFile(path) as archive:
        assert not any(name.startswith("objects/artifacts/") for name in archive.namelist())
        BundleReader(archive).verify()

    target = MemoryDatabase()
    target_objects = LocalObjectStore(tmp_path / "target")
    assert (await import_history(target, target_objects, path))["artifacts"] == 1
    artifact = Artifact.model_validate(target.rows["artifacts"][0])
    assert artifact.size_bytes == len(patch) and artifact.uri == HISTORY_MISSING_ARTIFACT_URI
    assert artifact.missing_reason == "文件缺失（源机器上已清理）"
    assert not await target_objects.list(f"tasks/{task}/")
    assert not any((await import_history(target, target_objects, path)).values())
    assert (await export_history(target, target_objects, tmp_path / "again.zip")).missing_objects == (
        manifest.missing_objects
    )


async def test_0025_round_trip_preserves_dsh_champion_backend(exported, tmp_path):
    path, _, _, _, _, source = exported
    source.revision = "0025"
    source.rows["champions"][0]["agent_backend"] = "dsh"
    manifest = await export_history(source, LocalObjectStore(tmp_path / "origin"), path)
    assert manifest.schema_revision == "0025"
    target = MemoryDatabase(revision="0025")
    await import_history(target, LocalObjectStore(tmp_path / "target"), path)
    assert target.rows["champions"][0]["agent_backend"] == "dsh"


async def test_import_accepts_original_bundle_without_missing_objects_or_backend(exported, tmp_path):
    path, manifest, *_ = exported
    legacy = tmp_path / "legacy.zip"
    data = manifest.model_dump(mode="json", exclude={"missing_objects"})
    data["counts"].pop("missing_objects")
    with ZipFile(path) as original, ZipFile(legacy, "w") as out:
        for name in manifest.files:
            payload = original.read(name)
            if name == "tables/champions.jsonl":
                row = json.loads(payload)
                assert row.pop("agent_backend") == "opencode"
                payload = json_line(row)
                data["files"][name] = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            out.writestr(name, payload)
        out.writestr("manifest.json", json.dumps(data))

    target = MemoryDatabase(revision="0025")
    assert (await import_history(target, LocalObjectStore(tmp_path / "target"), legacy))["objects"] == 2
    assert target.rows["champions"][0]["agent_backend"] == "opencode"


def test_cli_summarizes_missing_artifacts_and_strict_export_keeps_previous_zip(exported, tmp_path, monkeypatch, capsys):
    path, _, task, _, _, db = exported
    origin = LocalObjectStore(tmp_path / "origin")
    origin._path(key_for(task, "patch.diff")).unlink()
    monkeypatch.setattr(history_cli, "make_object_store", lambda settings: origin)
    monkeypatch.setattr(history_cli, "create_async_engine", lambda *args, **kwargs: db)
    previous = path.read_bytes()
    command = ["sca.history", "export", "--out", str(path)]
    monkeypatch.setattr(sys, "argv", [*command, "--strict"])
    with pytest.raises(SystemExit) as exit_info:
        history_cli.main()
    assert exit_info.value.code == 1 and path.read_bytes() == previous
    assert "严格模式下发现缺失的制品文件" in capsys.readouterr().err
    assert not list(tmp_path.glob(".sca-history-*"))

    monkeypatch.setattr(sys, "argv", command)
    history_cli.main()
    output = capsys.readouterr()
    assert "正在导出历史" in output.err
    assert "tasks 2" in output.out and "artifacts 1" in output.out
    assert '已复制 1 个对象，缺失 1 个对象' in output.out and '"patch.diff"' in output.out
    assert f"ZIP 大小为 {path.stat().st_size:,} 字节" in output.out


@pytest.mark.parametrize(("revision", "label"), [("0023", "0023"), (None, "未迁移")])
def test_cli_reports_incompatible_source_revision(revision, label, monkeypatch, tmp_path, capsys):
    db = MemoryDatabase(revision=revision)
    monkeypatch.setattr(history_cli, "create_async_engine", lambda *args, **kwargs: db)
    monkeypatch.setenv("SCA_OBJECT_STORE", "local")
    monkeypatch.setenv("SCA_DATA_DIR", str(tmp_path))
    path = tmp_path / "history.zip"
    monkeypatch.setattr(sys, "argv", ["sca.history", "export", "--out", str(path)])
    with pytest.raises(SystemExit) as exit_info:
        history_cli.main()
    assert exit_info.value.code == 1 and not path.exists()
    error = capsys.readouterr().err
    assert f"源库版本 {label}，需先升级到 0024" in error and "Traceback" not in error


def test_cli_progress_reports_every_batch_or_five_seconds(monkeypatch, capsys):
    timers = [Mock() for _ in range(3)]
    loop = SimpleNamespace(call_later=Mock(side_effect=timers))
    monkeypatch.setattr(history_cli.asyncio, "get_running_loop", lambda: loop)
    with history_cli._export_progress() as progress:
        assert "已写入 0 条记录" in capsys.readouterr().err
        for _ in range(499):
            progress("rows")
        assert not capsys.readouterr().err
        progress("rows")
        assert "已写入 500 条记录" in capsys.readouterr().err
        progress("objects")
        interval, report = loop.call_later.call_args.args
        assert interval == 5
        report()
        assert "已检查 1 个对象" in capsys.readouterr().err
    for timer in timers:
        timer.cancel.assert_called_once_with()


async def test_valid_manifest_does_not_allow_overlapping_import(exported, tmp_path):
    path, _, _, _, _, _ = exported
    target = MemoryDatabase(locked=True)
    objects = LocalObjectStore(tmp_path / "destination")
    with pytest.raises(HistoryError, match="Another history import"):
        await import_history(target, objects, path)
    assert not target.commands and not target.rows
    assert not objects.root.exists()


@pytest.mark.parametrize("member", ["tables/tasks.jsonl", "objects"])
async def test_checksum_failure_precedes_all_writes(exported, tmp_path, member):
    path, manifest, _, _, _, _ = exported
    if member == "objects":
        member = next(name for name in manifest.files if name.startswith("objects/artifacts/"))
    modified = tmp_path / "modified.zip"
    with ZipFile(path) as original, ZipFile(modified, "w") as out:
        for name in original.namelist():
            data = original.read(name)
            out.writestr(name, data + b" " if name == member else data)

    target = MemoryDatabase()
    objects = LocalObjectStore(tmp_path / "destination")
    with pytest.raises(HistoryError, match="Invalid|checksum"):
        await import_history(target, objects, modified)
    assert not target.rows
    assert not objects.root.exists()


async def test_object_write_failure_rolls_back_and_can_retry(exported, tmp_path, monkeypatch):
    path, _, _, _, _, _ = exported
    target = MemoryDatabase()
    objects = LocalObjectStore(tmp_path / "destination")
    write = transfer.write_object

    def fail_artifact(store, key, *args):
        if "/history-artifacts/" in key:
            raise OSError("simulated outage")
        return write(store, key, *args)

    with monkeypatch.context() as patch:
        patch.setattr(transfer, "write_object", fail_artifact)
        with pytest.raises(OSError, match="simulated outage"):
            await import_history(target, objects, path)
    assert not target.rows and await objects.list("") == []
    assert (await import_history(target, objects, path))["objects"] == 2


def test_archive_reader_refuses_unreferenced_paths(exported, tmp_path):
    path, manifest, *_ = exported
    modified = tmp_path / "unexpected.zip"
    other = "../escape"
    with ZipFile(path) as original, ZipFile(modified, "w") as out:
        for name in manifest.files:
            out.writestr(name, original.read(name))
        manifest.files[other] = next(iter(manifest.files.values()))
        manifest.counts["objects"] += 1
        out.writestr(other, b"not an object")
        out.writestr("manifest.json", manifest.model_dump_json())
    with ZipFile(modified) as archive, pytest.raises(HistoryError, match="unreferenced objects"):
        BundleReader(archive).verify()


@pytest.mark.parametrize("marked_missing", [False, True])
def test_archive_requires_each_artifact_to_be_either_present_or_marked_missing(exported, tmp_path, marked_missing):
    path, manifest, task, *_ = exported
    invalid = tmp_path / "invalid.zip"
    data = manifest.model_dump(mode="json")
    if marked_missing:
        data["missing_objects"] = [{"task_id": str(task), "name": "patch.diff"}]
        data["counts"]["missing_objects"] = 1
    else:
        member = next(name for name in manifest.files if name.startswith("objects/artifacts/"))
        del data["files"][member]
        data["counts"]["objects"] -= 1
    with ZipFile(path) as original, ZipFile(invalid, "w") as out:
        for name in data["files"]:
            out.writestr(name, original.read(name))
        out.writestr("manifest.json", json.dumps(data))
    with ZipFile(invalid) as out, pytest.raises(HistoryError, match="missing|Missing"):
        BundleReader(out).verify()


def test_schema_compatibility_checks_ancestry():
    require_ancestor("0024", "0024")
    require_ancestor("0017", "0020")  # 0020 precedes 0017 in the Alembic graph.
    with pytest.raises(HistoryError, match="include"):
        require_ancestor("0020", "0017")
    with pytest.raises(HistoryError, match="schema"):
        require_ancestor("0024", "002")


def test_redaction_preserves_usage_and_multiline_artifacts():
    value = clean_value({
        "max_tokens": 100, "tokens": 20, "input_tokens": 12, "apiTokens": ["fixture-api"],
        "billing_usage": {"value": "fixture-billing"}, "virtual_key": "fixture-virtual",
        "nested": [{"repo_url": "https://test:fixture-credential@host.invalid/repo"}],
        "comment": "custom_password=fixture-password",
    })
    assert value == {"max_tokens": 100, "tokens": 20, "input_tokens": 12,
                     "nested": [{"repo_url": "https://host.invalid/repo"}],
                     "comment": "custom_password=[redacted]"}
    log = io.BytesIO(b"Provider API_KEY=fixture-provider\r\n-----BEGIN OPENSSH PRIVATE KEY-----\r\n"
                     b"fixture-private-key\n-----END OPENSSH PRIVATE KEY-----\r\ndone\r\n")
    out = io.BytesIO()
    copy_text(log, out)
    assert b"fixture-" not in out.getvalue() and out.getvalue().endswith(b"done\r\n")
    assert clean_text('{"virtualKey": "fixture-value"}') == '{"virtualKey": "[redacted]"}'
    assert clean_text('{"Authorization": "Basic Zml4dHVyZQ=="}') == '{"Authorization": "[redacted]"}'
    assert clean_text("API_KEY=x") == "API_KEY=*"


def test_since_and_windows_artifact_keys():
    assert _since("2026-11-22") == datetime(2026, 11, 22, tzinfo=UTC)
    assert _since("2026-11-22T18:00:00+08:00") == NOW
    task = uuid4()
    uppercase = history_artifact_key(task, "figures/CON:Report.svg")
    lowercase = history_artifact_key(task, "figures/con:report.svg")
    assert uppercase != lowercase and uppercase.startswith(f"tasks/{task}/history-artifacts/")
    assert not set(uppercase) & set("\\:*?")


def test_s3_source_and_destination_streaming(tmp_path):
    class S3Client:
        def __init__(self, payloads):
            self.payloads = payloads

        def get_object(self, *, Bucket, Key):
            if Key not in self.payloads:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            data = self.payloads[Key]
            return {"Body": StreamingBody(io.BytesIO(data), len(data)), "ContentLength": len(data)}

        def upload_fileobj(self, source, bucket, key, *, ExtraArgs, Config):
            assert ExtraArgs == {"ContentType": "text/plain"} and not Config.use_threads
            self.payloads[key] = source.read()

    def store(payloads):
        adapter = object.__new__(S3ObjectStore)
        adapter.bucket = "fixture"
        adapter.client = S3Client(payloads)
        adapter._ensure_bucket = lambda: None
        return adapter

    payload = b"done\nAPI_KEY=fixture-provider\n"
    source, target = store({"report.log": payload}), store({})
    path = tmp_path / "artifact.zip"
    with ZipFile(path, "w") as archive:
        bundle = BundleWriter(archive)
        copy_artifact(source, "report.log", "report.log", "text/plain", len(payload), bundle, "object")
        member = bundle.files["object"]
    with ZipFile(path) as archive:
        assert write_object(target, "other-key", archive, "object", member, "text/plain")
        assert not write_object(target, "other-key", archive, "object", member, "text/plain")
    assert target.client.payloads["other-key"] == b"done\nAPI_KEY=[redacted]\n"


def test_s3_credential_metadata_check_never_fetches_contents(tmp_path):
    store = object.__new__(S3ObjectStore)
    store.bucket = "fixture"
    store.client = SimpleNamespace(head_object=Mock(), get_object=Mock())
    path = tmp_path / "credentials.zip"
    with ZipFile(path, "w") as archive:
        bundle = BundleWriter(archive)
        copy_artifact(store, "present", ".env", "text/plain", 4, bundle, "present")
        store.client.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
        with pytest.raises(NotFoundError):
            copy_artifact(store, "absent", ".env", "text/plain", 4, bundle, "absent")
    store.client.get_object.assert_not_called()
    with ZipFile(path) as archive:
        assert archive.namelist() == ["present"]
        assert archive.read("present") == b"[redacted credential file]\n"


@pytest.mark.parametrize("code", ["NoSuchBucket", "AccessDenied"])
def test_s3_export_propagates_storage_configuration_errors(code):
    store = object.__new__(S3ObjectStore)
    store.bucket = "fixture"
    store.client = SimpleNamespace(get_object=Mock(side_effect=ClientError({"Error": {"Code": code}}, "GetObject")))
    with pytest.raises(ClientError, match=code), read_object(store, "some-key"):
        pytest.fail("The source bucket or its permissions must be fixed")
