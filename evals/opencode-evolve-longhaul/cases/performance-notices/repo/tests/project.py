"""Application under test."""

from framehouse.api import request
from framehouse.registry import SCOPE_TABLE
from framehouse.runtime import APP

__all__ = ("APP", "EXAMPLE_TABLE", "PROGRAM", "SCOPE_TABLE", "request")

PROGRAM = "framehouse"
EXAMPLE_TABLE = "calls"
