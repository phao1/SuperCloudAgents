"""Harbor Relay's command entry point."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from . import cli_exchange, cli_journal, cli_routes
from .encoding import as_bytes
from .errors import RelayError


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise RelayError(message)


def parser() -> Parser:
    root = Parser(prog="harborrelay")
    root.add_argument("--home", type=Path, required=True)
    commands = root.add_subparsers(dest="command", required=True)
    for module in (cli_journal, cli_routes, cli_exchange):
        module.register(commands)
    return root


def run(argv: Sequence[str], out: TextIO, err: TextIO) -> int:
    try:
        args = parser().parse_args(argv)
        result = as_bytes(args.action(args.home, args)).decode("utf-8")
    except RelayError as error:
        err.write(f"error: {error}\n")
        return 2
    out.write(result + "\n")
    return 0


def main() -> int:
    return run(sys.argv[1:], sys.stdout, sys.stderr)
