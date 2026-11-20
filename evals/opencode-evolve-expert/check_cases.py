"""Exercise each fixture's starter, reference and shallow fixes in clean copies."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

SUITE = Path(__file__).resolve().parent
LIMIT_SECONDS = 25
LEGACY = "Legacy regression suite passed."
PUBLIC_FAILED = "Public verification failed."
PUBLIC_PASSED = "Public verification passed."
FEATURE_FAILED = "Feature assertion failed."
FEATURE_PASSED = "Feature checks passed."


@dataclass(frozen=True)
class Result:
    code: int
    output: str
    seconds: float

    def confirms(self, code: int, *messages: str) -> bool:
        return self.code == code and set(messages).issubset(self.output.splitlines())


def check_repository(repository: Path, acceptance: Path) -> Result:
    with TemporaryDirectory(prefix="evolve-expert-check-") as scratch:
        work = Path(scratch) / "repo"
        shutil.copytree(
            repository, work,
            ignore=shutil.ignore_patterns("shallow-*", "__pycache__", "*.pyc", ".git"),
        )
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
                "PYTHONHASHSEED": "0",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LIMIT_SECONDS,
        )
        return Result(result.returncode, result.stdout, monotonic() - start)


def check_cases(solutions: Path, *, suite: Path = SUITE) -> int:
    cases = sorted(path.parent for path in (suite / "cases").glob("*/task.yaml"))
    passed = starters = references = shallow_fixes = shallow_total = 0
    slowest = 0.0
    for case in cases:
        reference = solutions / case.name
        shallows = sorted(path for path in reference.glob("shallow-*") if path.is_dir())
        shallow_total += len(shallows)
        if len(shallows) < 2:
            print(f"FAIL {case.name} | expected at least two shallow fixes, found {len(shallows)}")
            continue

        jobs = [
            ("starter", case / "repo", 1, (LEGACY, PUBLIC_FAILED)),
            ("reference", reference, 0, (LEGACY, PUBLIC_PASSED, FEATURE_PASSED)),
            *((path.name, path, 1, (LEGACY, PUBLIC_PASSED, FEATURE_FAILED)) for path in shallows),
        ]
        successful = []
        for label, repository, code, messages in jobs:
            try:
                result = check_repository(repository, case / "acceptance")
            except (OSError, SyntaxError, subprocess.TimeoutExpired) as error:
                print(f"FAIL {case.name}/{label} | {error}")
                continue
            slowest = max(slowest, result.seconds)
            if result.confirms(code, *messages):
                successful.append(label)
            else:
                print(f"FAIL {case.name}/{label} | exit {result.code}\n{result.output[-4000:]}")

        starters += "starter" in successful
        references += "reference" in successful
        rejected = sum(label.startswith("shallow-") for label in successful)
        shallow_fixes += rejected
        if len(successful) == len(jobs):
            passed += 1
            print(f"PASS {case.name} | starter rejected | reference passed | {rejected} shallow fixes rejected")

    print(f"{passed}/{len(cases)} cases passed")
    print(f"{starters}/{len(cases)} starters rejected after passing legacy tests")
    print(f"{references}/{len(cases)} references passed hidden acceptance")
    print(f"{shallow_fixes}/{shallow_total} shallow fixes passed public verification and failed hidden acceptance")
    print(f"Slowest hidden run {slowest:.3f}s")
    return int(not cases or passed != len(cases))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("solutions", type=Path)
    args = parser.parse_args()
    raise SystemExit(check_cases(args.solutions.resolve()))


if __name__ == "__main__":
    main()
