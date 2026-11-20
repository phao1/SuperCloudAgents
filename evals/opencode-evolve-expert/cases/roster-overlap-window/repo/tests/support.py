import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from crewcall import assignments, settings
from crewcall.cli import run
from crewcall.desk import Desk

MON = "2024-05-06"
TUE = "2024-05-07"
WED = "2024-05-08"
FRI = "2024-05-10"


def fixture(path):
    desk = Desk(path)
    for args in (("main", "Main Hall", "UTC"), ("studio", "Berlin studio", "Europe/Berlin")):
        desk.change(settings.venue, *args)
    for args in (
        ("ada", "Ada", ["door", "sound"]), ("lin", "Lin", ["door"]), ("pat", "Pat", ["sound"]),
    ):
        desk.change(settings.member, *args)
    for args in (("doors", "main", "door", "Doors"), ("mix", "studio", "sound", "Mix")):
        desk.change(settings.lane, *args)
    return desk


class DeskTest(TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.path = Path(workspace.name) / "desk.json"
        self.desk = fixture(self.path)

    def rule(self, code="open", lane="doors", weekdays="mon", start="10:00", minutes=120,
             first=MON, through=None):
        return self.desk.change(settings.rule, code, lane, weekdays, start, minutes, first, through)

    def assign(self, shift="open:" + MON, member="ada"):
        return self.desk.change(assignments.add, shift, member)

    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        code = run(["--desk", str(self.path), *arguments], out, err)
        return code, out.getvalue(), err.getvalue()

    def json_cli(self, *arguments):
        code, out, error = self.cli(*arguments)
        self.assertEqual((code, error), (0, ""))
        self.assertTrue(out.endswith("\n"))
        return json.loads(out)

    def cli_error(self, message, *arguments):
        self.assertEqual(self.cli(*arguments), (2, "", f"error: {message}\n"))
