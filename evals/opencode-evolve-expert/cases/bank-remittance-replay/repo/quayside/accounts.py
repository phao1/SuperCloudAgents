"""Maintain an account directory with a fixed currency per code."""

from dataclasses import replace

from . import money, validation
from .errors import LedgerError
from .model import Account, State


def get(state: State, code: str) -> Account:
    identity = validation.code(code)
    try:
        return state.accounts[identity]
    except KeyError:
        raise LedgerError("unknown account") from None


def put(state: State, code: str, name: str, currency: str) -> tuple[State, Account]:
    record = Account(validation.code(code), validation.label(name, "account name"),
                     money.currency(currency))
    previous = state.accounts.get(record.code)
    if previous and previous.currency != record.currency:
        raise LedgerError("account currency cannot change")
    return replace(state, accounts={**state.accounts, record.code: record}), record


def listing(state: State) -> list[Account]:
    return [state.accounts[code] for code in sorted(state.accounts)]
