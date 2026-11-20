import csv
import io
import subprocess
import sys

from tests.support import FRIDAY, MONDAY, TUESDAY, Case


class CommandTests(Case):
    def test_module_entry_point_opens_existing_database(self):
        result = subprocess.run([sys.executable, "-B", "-m", "stockroom", "--db", str(self.filename),
                                 "item", "show", "ax"], capture_output=True, check=False, text=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '{"sku":"AX","name":"Axes","category":"TOOLS","unit":"each","active":true}\n')
        self.assertEqual(result.stderr, "")

    def test_catalog_commands_use_normalized_output_and_independent_filters(self):
        payload = self.json_cli("item", "add", "wire-3", "Câble", "--unit", "coil", "--category", "tools")
        self.assertEqual(payload, {"sku": "WIRE-3", "name": "Câble", "category": "TOOLS",
                                   "unit": "coil", "active": True})
        self.assertFalse(self.json_cli("item", "status", "AX", "inactive")["active"])
        self.assertEqual([row["sku"] for row in self.json_cli("item", "list", "--category", "tools")], ["WIRE-3"])
        self.assertEqual([row["sku"] for row in self.json_cli("item", "list", "--category", "tools", "--all")],
                         ["AX", "WIRE-3"])

    def test_catalog_show_error_uses_stderr(self):
        self.cli_error("unknown sku", "item", "show", "none")

    def test_warehouse_and_supplier_commands_use_existing_status(self):
        self.assertEqual(self.json_cli("warehouse", "add", "shed", "Back shed")["code"], "SHED")
        self.assertFalse(self.json_cli("warehouse", "status", "shed", "inactive")["active"])
        self.assertEqual([r["code"] for r in self.json_cli("warehouse", "list", "--all")],
                         ["NORTH", "SHED", "SOUTH"])
        self.assertEqual(self.json_cli("supplier", "add", "SLS", "Supplier", "--contact", "buyer")["contact"],
                         "buyer")
        self.assertFalse(self.json_cli("supplier", "status", "sls", "inactive")["active"])
        self.assertEqual([r["code"] for r in self.json_cli("supplier", "list")], ["COOP"])

    def test_lot_receive_prints_the_legacy_fields_and_supplied_memo_goes_to_history(self):
        value = self.json_cli("lot", "receive", "ax", "north", "in-1", "2", MONDAY, "--memo", "first pallet")
        self.assertEqual(set(value), {"id", "sku", "warehouse", "code", "quantity", "received", "supplier"})
        self.assertEqual((value["sku"], value["code"], value["quantity"]), ("AX", "IN-1", 2))
        activity = self.json_cli("report", "activity", "--kind", "receive")
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0]["memo"], "first pallet")

    def test_lot_export_csv_keeps_headers_when_no_data(self):
        code, output, error = self.cli("lot", "export", "--format", "csv", "--sku", "seed")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(output, "id,sku,warehouse,code,quantity,received,supplier\r\n")

    def test_lot_adjust_then_export_filters_empty_lots_and_runs_csv(self):
        first = self.lot("x", quantity=4)
        updated = self.json_cli("lot", "adjust", "AX", "NORTH", "x", "-4", TUESDAY)
        self.assertEqual(updated["quantity"], 0)
        self.assertEqual(self.json_cli("lot", "list"), [])
        code, text, error = self.cli("lot", "export", "--format", "csv", "--include-empty")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(list(csv.DictReader(io.StringIO(text))), [
            {"id": str(first.id), "sku": "AX", "warehouse": "NORTH", "code": "X",
             "quantity": "0", "received": MONDAY, "supplier": ""},
        ])

    def test_move_and_ship_commands_commit_evicted_quantities(self):
        self.lot(quantity=6)
        moved = self.json_cli("lot", "move", "AX", "NORTH", "SOUTH", "5", TUESDAY)
        self.assertEqual([(row["warehouse"], row["quantity"]) for row in moved], [("SOUTH", 5)])
        shipped = self.json_cli("lot", "ship", "AX", "SOUTH", "4", FRIDAY)
        self.assertEqual(shipped[0]["quantity"], 1)
        self.assertEqual([(row["warehouse"], row["quantity"]) for row in self.json_cli("report", "balances")],
                         [("NORTH", 1), ("SOUTH", 1)])

    def test_csv_export_escapes_fields_and_preserves_embedded_carriage_return(self):
        self.lot(memo='text with "quotes", and\r separator')
        code, output, error = self.cli("report", "activity", "--format", "csv")
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(output.endswith("\r\n"))
        self.assertIn('"text with ""quotes"", and\r separator"', output)
        self.assertEqual(next(csv.DictReader(io.StringIO(output)))["memo"], 'text with "quotes", and\r separator')

    def test_count_commands_share_the_committed_count_state(self):
        a = self.lot("a", 7)
        session = self.json_cli("count", "open", "north", "cycle", MONDAY)
        identity = str(session["id"])
        self.assertEqual(self.json_cli("count", "record", identity, str(a.id), "5")["observed"], 5)
        self.assertIsNone(self.json_cli("count", "clear", identity, str(a.id))["observed"])
        self.assertEqual(self.json_cli("count", "sheet", identity, "--differences-only"), [])
        self.json_cli("count", "record", identity, str(a.id), "0")
        self.assertEqual(self.json_cli("count", "close", identity, TUESDAY)["status"], "closed")
        self.assertEqual(self.json_cli("lot", "list"), [])

    def test_note_commands_observe_archive_visibility(self):
        lot = self.lot()
        note = self.json_cli("note", "add", str(lot.id), MONDAY, "révisé")
        self.assertEqual(note["text"], "révisé")
        self.assertEqual(self.json_cli("note", "list", str(lot.id)), [note])
        archived = self.json_cli("note", "archive", str(note["id"]))
        self.assertEqual(self.json_cli("note", "list", str(lot.id)), [])
        self.assertEqual(self.json_cli("note", "list", str(lot.id), "--all"), [archived])

    def test_input_errors_leave_stdout_empty_and_inventory_unchanged(self):
        self.lot("a", 1)
        self.cli_error("insufficient stock", "lot", "ship", "AX", "NORTH", "2", TUESDAY)
        self.cli_error("invalid day", "lot", "receive", "AX", "NORTH", "b", "1", "not-a-day")
        self.assertEqual([row.code for row in self.inventory.lots()], ["A"])
        code, out, err = self.cli("report", "activity", "--limit", "not-an-int")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("invalid int value", err)
