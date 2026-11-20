"""Run each fixture's hidden acceptance against a clean starter and a reference."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

SUITE = Path(__file__).resolve().parent
LIMIT_SECONDS = 27


@dataclass(frozen=True)
class Result:
    code: int
    output: str
    seconds: float


def check_repository(repository: Path, acceptance: Path) -> Result:
    with tempfile.TemporaryDirectory(prefix="evolve-real-check-") as scratch:
        work = Path(scratch) / "repo"
        shutil.copytree(repository, work, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for path in work.rglob("*.py"):
            compile(path.read_bytes(), str(path.relative_to(work)), "exec")
        shutil.copyfile(acceptance / "hidden.py", work / "hidden.py")
        python_bin = str(Path(sys.executable).parent)
        start = monotonic()
        result = subprocess.run(
            [sys.executable, "-B", "hidden.py"],
            cwd=work,
            env={
                "PATH": f"{python_bin}{os.pathsep}{os.defpath}",
                "PYTHONUTF8": "1",
                "PYTHONNOUSERSITE": "1",
            },
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LIMIT_SECONDS,
        )
        return Result(result.returncode, (result.stdout + result.stderr)[-4000:], monotonic() - start)


def check_cases(solutions: Path) -> int:
    cases = sorted(path.parent for path in (SUITE / "cases").glob("*/task.yaml"))
    failures = 0
    slowest = 0.0
    for case in cases:
        try:
            starter = check_repository(case / "repo", case / "acceptance")
            reference = check_repository(solutions / case.name, case / "acceptance")
        except (OSError, SyntaxError, subprocess.TimeoutExpired) as error:
            failures += 1
            print(f"FAIL {case.name} {error}")
            continue

        slowest = max(slowest, starter.seconds, reference.seconds)
        starter_rejected = starter.code == 1 and "Public verification failed." in starter.output
        if (case / "repo/tests").is_dir():
            starter_rejected = starter_rejected and "Legacy regression suite passed." in starter.output
        passed = next(
            (line for line in reference.output.splitlines() if line.endswith(" feature checks passed.")),
            "",
        )
        if starter_rejected and reference.code == 0 and passed:
            print(f"PASS {case.name} | untouched fails | {passed}")
        else:
            failures += 1
            print(f"FAIL {case.name} | starter={starter.code} | reference={reference.code}")
            if not starter_rejected:
                print(starter.output)
            if reference.code != 0 or not passed:
                print(reference.output)

    print(f"{len(cases) - failures} passed, {failures} failed ({len(cases)} cases)")
    print(f"Slowest hidden run: {slowest:.3f}s")
    return int(not cases or failures > 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("solutions", type=Path)
    args = parser.parse_args()
    raise SystemExit(check_cases(args.solutions.resolve()))


if __name__ == "__main__":
    main()
