"""Commands for inventory summaries, ages, and audit history."""

from .cli_util import day_window, filters, format_option
from .history import HistoricalLot, History
from .models import Aging, AuditLine, Balance, Movement, Trend
from .movement_rows import KINDS
from .output import render
from .reporting import Reporting
from .trend import PERIODS


def register(commands) -> None:
    group = commands.add_parser("report", help="read inventory and movement reports")
    actions = group.add_subparsers(dest="action", required=True)

    totals = actions.add_parser("balances")
    filters(totals)
    format_option(totals)
    totals.set_defaults(action_fn=balances)

    history = actions.add_parser("activity")
    filters(history)
    format_option(history)
    day_window(history)
    history.add_argument("--kind", choices=KINDS)
    history.add_argument("--limit", type=int)
    history.set_defaults(action_fn=activity)

    ages = actions.add_parser("aging")
    ages.add_argument("day")
    filters(ages)
    format_option(ages)
    ages.set_defaults(action_fn=aging)

    series = actions.add_parser("trend")
    filters(series)
    format_option(series)
    day_window(series)
    series.add_argument("--period", choices=PERIODS, default="day")
    series.set_defaults(action_fn=trend)

    past = actions.add_parser("as-of")
    past.add_argument("day")
    filters(past)
    format_option(past)
    past.add_argument("--include-zero", action="store_true")
    past.set_defaults(action_fn=as_of)

    review = actions.add_parser("audit")
    filters(review)
    format_option(review)
    review.add_argument("--mismatches-only", action="store_true")
    review.set_defaults(action_fn=audit)


def balances(connection, args) -> str:
    return render(Reporting(connection).balances(args.sku, args.warehouse), Balance, args.format)


def activity(connection, args) -> str:
    rows = Reporting(connection).activity(args.sku, args.warehouse, args.since, args.through,
                                          args.kind, args.limit)
    return render(rows, Movement, args.format)


def aging(connection, args) -> str:
    return render(Reporting(connection).aging(args.day, args.sku, args.warehouse), Aging, args.format)


def audit(connection, args) -> str:
    rows = Reporting(connection).audit(args.sku, args.warehouse, args.mismatches_only)
    return render(rows, AuditLine, args.format)


def trend(connection, args) -> str:
    rows = Reporting(connection).trend(args.period, args.sku, args.warehouse, args.since, args.through)
    return render(rows, Trend, args.format)


def as_of(connection, args) -> str:
    rows = History(connection).lots_at(args.day, args.sku, args.warehouse, args.include_zero)
    return render(rows, HistoricalLot, args.format)
