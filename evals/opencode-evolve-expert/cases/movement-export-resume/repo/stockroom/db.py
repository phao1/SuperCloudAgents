"""Opening a store also initializes a new local warehouse database."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import StockroomError

SCHEMA = Path(__file__).with_name("schema.sql")


def open_store(filename: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(filename)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        version, = connection.execute("PRAGMA user_version").fetchone()
        if version == 0:
            connection.executescript(SCHEMA.read_text(encoding="utf-8"))
        elif version != 1:
            raise StockroomError("unsupported store version")
    except Exception:
        connection.close()
        raise
    return connection


@contextmanager
def store(filename: str | Path) -> Iterator[sqlite3.Connection]:
    connection = open_store(filename)
    try:
        yield connection
    finally:
        connection.close()
