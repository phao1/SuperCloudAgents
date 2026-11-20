"""Crewcall read/write commands and their error convention."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from . import cli_board, cli_export, cli_roster, cli_settings, storage
from .desk import Desk
from .errors import DeskError


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DeskError(message)


def parser() -> Parser:
    root = Parser(prog="crewcall")
    root.add_argument("--desk", type=Path, required=True)
    tools = root.add_subparsers(dest="group", required=True)
    for commands in (cli_settings, cli_roster, cli_export, cli_board):
        commands.register(tools)
    return root


def run(argv: Sequence[str], stdout: TextIO, stderr: TextIO) -> int:
    try:
        args = parser().parse_args(argv)
        value = args.handler(Desk(args.desk), args)
        rendered = value if isinstance(value, str) else storage.text(value)
    except DeskError as error:
        stderr.write(f"error: {error}\n")
        return 2
    stdout.write(rendered)
    return 0


def main() -> int:
    return run(sys.argv[1:], sys.stdout, sys.stderr)
