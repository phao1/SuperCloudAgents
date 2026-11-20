"""Check untouched failures and reference successes without Docker or third-party deps.

Usage: .venv/bin/python evals/opencode-evolve-hard/check_cases.py .data/evolve-hard-solutions
Reference directories are complete repositories named after case IDs. Never put
solutions in this suite: its hash and uploads must contain fixtures only.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def check_repository(repository: Path, acceptance: Path) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="evolve-check-") as scratch:
        work = Path(scratch) / "repo"
        shutil.copytree(repository, work, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for path in work.rglob("*.py"):
            try:
                compile(path.read_bytes(), str(path.relative_to(work)), "exec")
            except SyntaxError as error:
                return False, f"invalid repository source: {error}"
        shutil.copyfile(acceptance / "hidden.py", work / "hidden.py")
        result = subprocess.run(
            [sys.executable, "-B", "hidden.py"], cwd=work,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(work)},
            capture_output=True, text=True, timeout=19,
        )
        return result.returncode == 0, (result.stdout + result.stderr)[-4000:]


def check_cases(solutions: Path) -> int:
    cases = sorted((Path(__file__).resolve().parent / "cases").iterdir())
    failures = 0
    for case in cases:
        try:
            untouched, original_output = check_repository(case / "repo", case / "acceptance")
            reference, reference_output = check_repository(solutions / case.name, case / "acceptance")
            valid = not untouched and reference and "invalid repository source:" not in original_output
            if valid:
                print(f"PASS {case.name}: untouched fails, reference passes")
            else:
                failures += 1
                print(f"FAIL {case.name}: untouched={untouched}, reference={reference}")
                print(original_output if "invalid repository source:" in original_output else reference_output)
        except (OSError, subprocess.TimeoutExpired) as error:
            failures += 1
            print(f"FAIL {case.name}: {error}")
    print(f"{len(cases) - failures} passed, {failures} failed ({len(cases)} cases)")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("solutions", type=Path)
    args = parser.parse_args()
    raise SystemExit(bool(check_cases(args.solutions.resolve())))


if __name__ == "__main__":
    main()
