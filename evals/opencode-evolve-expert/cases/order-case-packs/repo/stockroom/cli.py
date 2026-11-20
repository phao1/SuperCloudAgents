"""Command-line entry point with a shared error convention."""

import sqlite3
import sys
from collections.abc import Sequence
from typing import TextIO

from . import (
    cli_catalog,
    cli_files,
    cli_fulfillment,
    cli_lots,
    cli_notes,
    cli_reports,
    cli_stocktake,
)
from .cli_util import Parser
from .db import store
from .errors import StockroomError


def parser() -> Parser:
    root = Parser(prog="stockroom", description="Manage a local warehouse inventory")
    root.add_argument("--db", required=True, help="SQLite database filename")
    commands = root.add_subparsers(dest="group", required=True)
    for group in (cli_catalog, cli_lots, cli_reports, cli_files, cli_notes, cli_stocktake, cli_fulfillment):
        group.register(commands)
    return root


def run(argv: Sequence[str], stdout: TextIO, stderr: TextIO) -> int:
    try:
        args = parser().parse_args(argv)
        with store(args.db) as connection:
            result = args.action_fn(connection, args)
    except (StockroomError, sqlite3.Error) as error:
        stderr.write(f"error: {error}\n")
        return 2
    stdout.write(result)
    return 0


def main() -> int:
    return run(sys.argv[1:], sys.stdout, sys.stderr)
