"""Command-line adapter. It builds argparse entries directly from the registry."""

import argparse
import json
import sys
from pathlib import Path

from .application import Application, Command, Reply
from .errors import DeskError
from .fields import Field, Row


def _attach(parser: argparse.ArgumentParser, field: Field) -> None:
    parser.add_argument(
        field.option,
        dest=field.name,
        required=field.required,
        help=field.help,
        default=argparse.SUPPRESS,
    )


def make_parser(app: Application, program: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=program)
    parser.add_argument("--state", type=Path, default=Path("desk.json"), help="workspace file")
    parser.add_argument("--actor", default="lead", help="desk login name")
    parser.add_argument(
        "--locale", default="en", choices=tuple(app.messages), help="response language"
    )
    groups = parser.add_subparsers(dest="group", required=True)
    by_group = {}
    for command in app.registry:
        if command.group not in by_group:
            group = groups.add_parser(command.group)
            by_group[command.group] = group.add_subparsers(dest="verb", required=True)
        verbs = by_group[command.group]
        verb = verbs.add_parser(command.verb, help=command.help, description=command.help)
        for field in command.fields:
            _attach(verb, field)
        verb.set_defaults(command=command)
    return parser


def _params(command: Command, args: argparse.Namespace) -> Row:
    values = vars(args)
    return {
        field.name: field.cli(values[field.name])
        for field in command.fields
        if field.name in values
    }


def _print(reply: Reply) -> int:
    stream = sys.stdout if reply.status == 200 else sys.stderr
    print(json.dumps(reply.body, ensure_ascii=False, sort_keys=True), file=stream)
    if reply.status == 200:
        return 0
    return 2 if reply.status == 400 else 1


def main(app: Application, program: str, argv: list[str] | None = None) -> int:
    args = make_parser(app, program).parse_args(argv)
    command = args.command
    try:
        params = _params(command, args)
    except DeskError as error:
        return _print(app.error(error, args.locale))
    reply = app.request(
        args.state, args.actor, command.method, command.path, params, locale=args.locale
    )
    return _print(reply)
