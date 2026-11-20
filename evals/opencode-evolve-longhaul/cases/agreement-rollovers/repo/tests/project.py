"""Application under test."""

from groveboard.api import request
from groveboard.registry import SCOPE_TABLE
from groveboard.runtime import APP

__all__ = ("APP", "EXAMPLE_TABLE", "PROGRAM", "SCOPE_TABLE", "request")

PROGRAM = "groveboard"
EXAMPLE_TABLE = "purchase-orders"
