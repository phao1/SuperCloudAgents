"""Small argparse conventions shared between command groups."""

from argparse import ArgumentParser

from .errors import StockroomError


class Parser(ArgumentParser):
    def error(self, message: str) -> None:
        raise StockroomError(message)


def filters(parser: ArgumentParser) -> None:
    parser.add_argument("--sku", help="filter by item SKU")
    parser.add_argument("--warehouse", help="filter by warehouse code")


def format_option(parser: ArgumentParser) -> None:
    parser.add_argument("--format", choices=("json", "csv"), default="json")


def day_window(parser: ArgumentParser) -> None:
    parser.add_argument("--since", help="include movements on or after this day")
    parser.add_argument("--through", help="include movements on or before this day")


def note_option(parser: ArgumentParser) -> None:
    parser.add_argument("--memo", default="", help="text to store with the movement")
