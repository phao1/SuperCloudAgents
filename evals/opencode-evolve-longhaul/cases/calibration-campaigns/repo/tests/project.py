"""Application under test."""

from watershed.api import request
from watershed.registry import SCOPE_TABLE
from watershed.runtime import APP

__all__ = ("APP", "EXAMPLE_TABLE", "PROGRAM", "SCOPE_TABLE", "request")

PROGRAM = "watershed"
EXAMPLE_TABLE = "visits"
