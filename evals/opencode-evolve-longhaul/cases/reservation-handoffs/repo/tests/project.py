"""Application under test."""

from workyard.api import request
from workyard.registry import SCOPE_TABLE
from workyard.runtime import APP

__all__ = ("APP", "EXAMPLE_TABLE", "PROGRAM", "SCOPE_TABLE", "request")

PROGRAM = "workyard"
EXAMPLE_TABLE = "reservations"
