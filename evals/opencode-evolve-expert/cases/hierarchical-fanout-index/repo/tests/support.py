import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from harborrelay.cli import run
from harborrelay.directory import Directory
from harborrelay.journal import Journal
from harborrelay.types import Envelope, Notice


def notice(stream="dock.arrivals", key="crate", stamp=0, kind="landed", **value):
    return Notice(stream, kind, key, value, stamp)


def event(seq: int, stream="dock.arrivals", **value):
    return Envelope(seq, stream, "landed", "crate", value, 0)


class DeskTest(TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.home = self.root / "desk"
        self.journal = Journal(self.home)
        self.directory = Directory(self.home)

    def run_cli(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = run(["--home", str(self.home), *args], out, err)
        return code, out.getvalue(), err.getvalue()

    def json_cli(self, *args: str):
        code, out, err = self.run_cli(*args)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.endswith("\n"))
        return json.loads(out)

    def cli_error(self, text: str, *args: str):
        code, out, err = self.run_cli(*args)
        self.assertEqual((code, out, err), (2, "", f"error: {text}\n"))
