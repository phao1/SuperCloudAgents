from stockroom.errors import Missing, StockroomError
from stockroom.notes import Notes

from tests.support import MONDAY, THURSDAY, TUESDAY, Case


class NoteTests(Case):
    def test_note_ids_and_contents_are_preserved(self):
        notes = Notes(self.connection)
        lot = self.lot()
        text = "  check wrap\n雪  "
        added = notes.add(lot.id, MONDAY, text)
        self.assertEqual((added.lot_id, added.text, added.archived), (lot.id, text, False))
        self.assertEqual(notes.list(lot.id), [added])
        self.assertEqual(self.inventory.rows.by_id(lot.id).quantity, 5)

    def test_notes_are_sorted_by_date_then_id(self):
        notes = Notes(self.connection)
        lot = self.lot()
        later = notes.add(lot.id, THURSDAY, "later")
        first = notes.add(lot.id, TUESDAY, "first")
        second = notes.add(lot.id, TUESDAY, "second")
        self.assertEqual(notes.list(lot.id), [first, second, later])

    def test_archive_is_idempotent_and_only_changes_visibility(self):
        notes = Notes(self.connection)
        lot = self.lot()
        added = notes.add(lot.id, MONDAY, "archive requested")
        preserved = notes.add(lot.id, MONDAY, "still current")
        archived = notes.archive(added.id)
        self.assertTrue(archived.archived)
        self.assertEqual((archived.id, archived.text), (added.id, added.text))
        self.assertEqual(notes.archive(added.id), archived)
        self.assertEqual(notes.list(lot.id), [preserved])
        self.assertEqual(notes.list(lot.id, True), [archived, preserved])

    def test_notes_reject_nonexistent_lots_and_notes(self):
        notes = Notes(self.connection)
        self.lot()
        with self.assertRaisesRegex(Missing, "unknown lot"):
            notes.add(50, MONDAY, "legitimate")
        with self.assertRaisesRegex(Missing, "unknown lot"):
            notes.list(50)
        with self.assertRaisesRegex(Missing, "unknown note"):
            notes.archive(50)

    def test_notes_validate_day_text_and_id(self):
        notes = Notes(self.connection)
        lot = self.lot()
        for arguments, message in [
            ((True, MONDAY, "x"), "invalid quantity"), ((lot.id, "24-01-01", "x"), "invalid day"),
            ((lot.id, MONDAY, " \t"), "empty note"), ((lot.id, MONDAY, "x" * 401), "invalid memo"),
        ]:
            with self.subTest(args=arguments), self.assertRaisesRegex(StockroomError, message):
                notes.add(*arguments)
        self.assertEqual(notes.list(lot.id), [])

    def test_notes_stay_with_the_lot_after_it_reaches_zero(self):
        notes = Notes(self.connection)
        lot = self.lot("LAST", 1)
        message = notes.add(lot.id, MONDAY, "last unit")
        self.inventory.ship("AX", "NORTH", 1, TUESDAY)
        self.assertEqual(self.inventory.lots(), [])
        self.assertEqual(notes.list(lot.id), [message])
        self.assertEqual(self.inventory.lots(include_empty=True)[0].id, lot.id)
