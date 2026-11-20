"""Utilities for exercising a saved desk with independent entry points."""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


class DeskTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = TemporaryDirectory(prefix="desk-regression-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.state = self.root / "workspace.json"
        shutil.copyfile(ROOT / "fixtures/workspace-v1.json", self.state)

    def request(self, method, route, params=None, *, actor="lead", locale="en", status=200):
        from tests.project import request

        reply = request(self.state, actor, method, "/v1/" + route, params, locale=locale)
        self.assertEqual(reply.status, status, reply.body)
        return reply.body

    def get(self, route, params=None, **options):
        return self.request("GET", route, params, **options)["data"]

    def post(self, route, params, **options):
        return self.request("POST", route, params, **options)["data"]

    def cli(self, *args, actor="lead", locale="en", returncode=0):
        from tests.project import PROGRAM

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                PROGRAM,
                "--state",
                str(self.state),
                "--actor",
                actor,
                "--locale",
                locale,
                *args,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, returncode, (result.stdout, result.stderr))
        return json.loads(result.stdout if returncode == 0 else result.stderr)

    def state_data(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def assert_unchanged(self, before):
        self.assertEqual(self.state.read_bytes(), before)
