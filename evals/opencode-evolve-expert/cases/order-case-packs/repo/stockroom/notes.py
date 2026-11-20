"""Validate and attach lot notes without changing inventory counts."""

import sqlite3

from . import fields
from .errors import StockroomError
from .lot_rows import LotRows
from .models import Note
from .note_rows import NoteRows


class Notes:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.notes = NoteRows(connection)
        self.lots = LotRows(connection)

    def add(self, lot_id: int, at: str, text: str) -> Note:
        lot_id, at, text = fields.positive(lot_id), fields.day(at), fields.memo(text)
        if not text.strip():
            raise StockroomError("empty note")
        self.lots.by_id(lot_id)
        with self.connection:
            return self.notes.add(lot_id, at, text)

    def archive(self, note_id: int) -> Note:
        note_id = fields.positive(note_id)
        with self.connection:
            return self.notes.archive(note_id)

    def list(self, lot_id: int, include_archived: bool = False) -> list[Note]:
        lot_id = fields.positive(lot_id)
        self.lots.by_id(lot_id)
        return self.notes.list(lot_id, include_archived)
