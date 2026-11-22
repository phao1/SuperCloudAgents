"""Transfer SCA history using the operator database and object store."""

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from zipfile import BadZipFile
from zlib import error as CompressionError

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from sca.config import Settings
from sca.history.bundle import HistoryError
from sca.history.transfer import export_history, import_history
from sca.storage import make_object_store
from sca.storage.local import LocalObjectStore
from sca.storage.s3 import S3ObjectStore


def _since(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("请输入 ISO 8601 日期或时间，例如 2026-11-22") from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


async def _run(args: argparse.Namespace) -> None:
    settings = Settings(_env_file=None)
    objects = make_object_store(settings)
    if not isinstance(objects, (LocalObjectStore, S3ObjectStore)):
        raise HistoryError("Unsupported history object store")
    engine = create_async_engine(settings.database_url, hide_parameters=True)
    try:
        if args.command == "export":
            manifest = await export_history(
                engine, objects, args.out, tenants=tuple(args.tenant), since=args.since,
            )
            counts = manifest.counts
            action = "导出"
        else:
            counts = await import_history(
                engine, objects, args.file, default_quota=settings.default_max_active_tasks,
            )
            action = "导入"
        blobs = counts["objects"]
        rows = sum(counts.values()) - blobs
        print(f"已{action} {rows} 条历史记录和 {blobs} 个对象。")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m sca.history", description="在 SCA 实例之间导出和导入历史记录",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="导出历史 ZIP")
    export.add_argument("--out", type=Path, required=True, metavar="FILE")
    export.add_argument("--tenant", action="append", default=[], help="租户 ID，可多次指定")
    export.add_argument("--since", type=_since, help="ISO 8601 日期或时间，省略时区时使用 UTC")
    restore = commands.add_parser("import", help="校验并导入历史 ZIP")
    restore.add_argument("file", type=Path, metavar="FILE")
    try:
        asyncio.run(_run(parser.parse_args()))
    except HistoryError as exc:
        parser.exit(1, f"历史传输失败。{exc}\n")
    except (SQLAlchemyError, BotoCoreError, ClientError, OSError,
            BadZipFile, EOFError, CompressionError, ValueError):
        parser.exit(1, "历史传输失败。请检查数据库连接、对象存储配置和 ZIP 文件。\n")


if __name__ == "__main__":
    main()
