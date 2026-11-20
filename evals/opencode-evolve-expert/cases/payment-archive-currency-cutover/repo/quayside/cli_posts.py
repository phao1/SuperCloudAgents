"""Posting and account entry points."""

from pathlib import Path

from . import accounts, invoices, lookup, payments
from . import register as bank_register


def register(root) -> None:
    account = root.add_parser("account").add_subparsers(dest="action", required=True)
    command = account.add_parser("put")
    for name in ("code", "name", "currency"):
        command.add_argument(name)
    command.set_defaults(apply=put_account)
    account.add_parser("list").set_defaults(apply=list_accounts)

    invoice = root.add_parser("invoice").add_subparsers(dest="action", required=True)
    command = invoice.add_parser("issue")
    for name in ("account", "reference", "day"):
        command.add_argument(name)
    command.add_argument("path", type=Path)
    command.set_defaults(apply=issue)
    command = invoice.add_parser("show")
    command.add_argument("id", type=int)
    command.set_defaults(apply=show)
    command = invoice.add_parser("find")
    command.add_argument("account")
    command.add_argument("reference")
    command.set_defaults(apply=find)
    listing = invoice.add_parser("list")
    listing.add_argument("--account")
    listing.set_defaults(apply=list_invoices)

    payment = root.add_parser("payment").add_subparsers(dest="action", required=True)
    command = payment.add_parser("add")
    for name in ("account", "day", "amount", "reference"):
        command.add_argument(name)
    command.set_defaults(apply=deposit)
    command = payment.add_parser("bank-import")
    command.add_argument("path", type=Path)
    command.set_defaults(apply=bank)
    listing = payment.add_parser("list")
    listing.add_argument("--account")
    listing.set_defaults(apply=list_payments)
    register = payment.add_parser("register")
    register.add_argument("path", type=Path)
    register.add_argument("--account")
    register.add_argument("--since")
    register.add_argument("--through")
    register.set_defaults(apply=register_export)


def put_account(ledger, args):
    return ledger.change(accounts.put, args.code, args.name, args.currency)


def list_accounts(ledger, args):
    return accounts.listing(ledger.state)


def issue(ledger, args):
    return ledger.change(invoices.issue, args.account, args.reference, args.day, invoices.read_lines(args.path))


def show(ledger, args):
    return invoices.get(ledger.state, args.id)


def find(ledger, args):
    return lookup.find(ledger.state, args.account, args.reference)


def list_invoices(ledger, args):
    return invoices.listing(ledger.state, args.account)


def deposit(ledger, args):
    return ledger.change(payments.add, args.account, args.day, args.amount, args.reference)


def bank(ledger, args):
    return ledger.change(payments.import_bank, args.path)


def list_payments(ledger, args):
    return payments.listing(ledger.state, args.account)


def register_export(ledger, args):
    return {"exported": bank_register.dump(ledger.state, args.path, args.account, args.since, args.through)}
