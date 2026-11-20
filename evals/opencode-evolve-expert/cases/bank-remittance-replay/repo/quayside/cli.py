"""Quayside console rendering and error boundaries."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from . import cli_posts, cli_reports, storage
from .errors import LedgerError
from .ledger import Ledger


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise LedgerError(message)


def parser() -> Parser:
    root = Parser(prog="quayside")
    root.add_argument("--ledger", type=Path, required=True)
    commands = root.add_subparsers(dest="command", required=True)
    cli_posts.register(commands)
    cli_reports.register(commands)
    return root


def run(argv: Sequence[str], stdout: TextIO, stderr: TextIO) -> int:
    try:
        args = parser().parse_args(argv)
        found = args.apply(Ledger(args.ledger), args)
        text = found if isinstance(found, str) else storage.text(found)
    except LedgerError as error:
        stderr.write(f"error: {error}\n")
        return 2
    stdout.write(text)
    return 0


def main() -> int:
    return run(sys.argv[1:], sys.stdout, sys.stderr)
