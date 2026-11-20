"""SQL access for notes attached to a stable lot ID."""

import sqlite3

from .errors import Missing
from .models import Note
from .query import Conditions


class NoteRows:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @staticmethod
    def _note(row: sqlite3.Row) -> Note:
        return Note(row["id"], row["lot_id"], row["day"], row["text"], bool(row["archived"]))

    def add(self, lot_id: int, day: str, text: str) -> Note:
        created = self.connection.execute("INSERT INTO note(lot_id, day, text) VALUES (?, ?, ?)",
                                          (lot_id, day, text))
        return self.by_id(created.lastrowid)

    def by_id(self, note_id: int) -> Note:
        row = self.connection.execute(
            "SELECT id, lot_id, day, text, archived FROM note WHERE id = ?", (note_id,),
        ).fetchone()
        if row is None:
            raise Missing("unknown note")
        return self._note(row)

    def archive(self, note_id: int) -> Note:
        self.by_id(note_id)
        self.connection.execute("UPDATE note SET archived = 1 WHERE id = ?", (note_id,))
        return self.by_id(note_id)

    def list(self, lot_id: int, include_archived: bool = False) -> list[Note]:
        query = Conditions()
        query.equals("lot_id", lot_id)
        if not include_archived:
            query.equals("archived", 0)
        sql = "SELECT id, lot_id, day, text, archived FROM note" + query.where() + " ORDER BY day, id"
        return [self._note(row) for row in self.connection.execute(sql, query.arguments)]
