"""Rate, statement and exchange commands."""

from pathlib import Path

from . import aging, exchange, output, rates, statements


def register(root) -> None:
    rate = root.add_parser("rate").add_subparsers(dest="action", required=True)
    for label, final, handler in (("set", "value", set_rate), ("quote", "amount", quote)):
        command = rate.add_parser(label)
        for name in ("source", "target", "day", final):
            command.add_argument(name)
        command.set_defaults(apply=handler)

    report = root.add_parser("statement").add_subparsers(dest="action", required=True)
    command = report.add_parser("open")
    command.add_argument("first")
    command.add_argument("last")
    command.add_argument("--currency")
    command.add_argument("--account")
    command.add_argument("--format", choices=("json", "csv"), default="json")
    command.set_defaults(apply=statement)
    summary = report.add_parser("aging")
    summary.add_argument("through")
    summary.add_argument("--account")
    summary.set_defaults(apply=aged)

    archive = root.add_parser("archive").add_subparsers(dest="action", required=True)
    command = archive.add_parser("export")
    command.add_argument("path", type=Path)
    command.add_argument("--account")
    command.set_defaults(apply=export)
    command = archive.add_parser("import")
    command.add_argument("path", type=Path)
    command.set_defaults(apply=restore)


def set_rate(ledger, args):
    return ledger.change(rates.put, args.source, args.target, args.day, args.value)


def quote(ledger, args):
    value = rates.quote(ledger.state, args.source, args.target, args.day, args.amount)
    return {"currency": args.target.upper(), "amount": value}


def statement(ledger, args):
    result = statements.project(ledger.state, args.first, args.last, args.currency, args.account)
    return output.statement_csv(result) if args.format == "csv" else result


def export(ledger, args):
    return {"exported": exchange.dump(ledger.state, args.path, args.account)}


def restore(ledger, args):
    return ledger.change(exchange.restore, args.path)


def aged(ledger, args):
    return aging.report(ledger.state, args.through, args.account)
