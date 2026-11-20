"""Check each longhaul starter, reference and partial implementation in clean copies."""

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
LIMIT_SECONDS = 35
LEGACY_PASSED = "Legacy regression suite passed."
PUBLIC_FAILED = "Public verification failed."
PUBLIC_PASSED = "Public verification passed."
FEATURE_PASSED = "Feature checks passed."
IGNORE = shutil.ignore_patterns("partial-*", "__pycache__", "*.pyc", ".git")


@dataclass(frozen=True)
class Result:
    code: int
    output: str
    seconds: float

    def confirms(self, code: int, *messages: str) -> bool:
        return self.code == code and set(messages).issubset(self.output.splitlines())


def check_repository(repository: Path, acceptance: Path) -> Result:
    with TemporaryDirectory(prefix="evolve-longhaul-check-") as directory:
        work = Path(directory) / "repo"
        shutil.copytree(repository, work, ignore=IGNORE)
        for path in work.rglob("*.py"):
            compile(path.read_bytes(), path.relative_to(work).as_posix(), "exec")
        shutil.copyfile(acceptance / "hidden.py", work / "hidden.py")
        start = monotonic()
        result = subprocess.run(
            [sys.executable, "-B", "hidden.py"],
            cwd=work,
            env={
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                "PYTHONUTF8": "1",
                "PYTHONHASHSEED": "0",
                "PYTHONNOUSERSITE": "1",
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


def file_contents(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and not any(part.startswith("partial-") or part in {"__pycache__", ".git"}
                    for part in path.relative_to(root).parts)
        and path.suffix != ".pyc"
    }


def changed_file_count(starter: Path, reference: Path) -> int:
    base, updated = file_contents(starter), file_contents(reference)
    return sum(base.get(path) != updated.get(path) for path in base.keys() | updated.keys())


def check_cases(solutions: Path, *, suite: Path = SUITE) -> int:
    cases = sorted(path.parent for path in (suite / "cases").glob("*/task.yaml"))
    passed = starters = references = partials = partial_total = 0
    slowest = 0.0
    for case in cases:
        repo = case / "repo"
        reference = solutions / case.name
        candidates = sorted(path for path in reference.glob("partial-*") if path.is_dir())
        partial_total += len(candidates)
        if len(candidates) < 2:
            print(f"FAIL {case.name} | expected two partial implementations, found {len(candidates)}")
            continue

        count = changed_file_count(repo, reference)
        if not 6 <= count <= 15:
            print(f"FAIL {case.name} | reference changes {count} files, expected 6–15")
            continue

        jobs = [
            ("starter", repo, 1, (LEGACY_PASSED, PUBLIC_FAILED)),
            ("reference", reference, 0, (LEGACY_PASSED, PUBLIC_PASSED, FEATURE_PASSED)),
            *((path.name, path, 1, (
                LEGACY_PASSED, PUBLIC_PASSED, f"Feature check failed: {path.name.removeprefix('partial-')}",
            )) for path in candidates),
        ]
        confirmed = set()
        for label, repository, code, messages in jobs:
            try:
                result = check_repository(repository, case / "acceptance")
            except (OSError, SyntaxError, subprocess.TimeoutExpired) as error:
                print(f"FAIL {case.name}/{label} | {error}")
                continue
            slowest = max(slowest, result.seconds)
            if result.confirms(code, *messages):
                confirmed.add(label)
            else:
                print(f"FAIL {case.name}/{label} | exit {result.code}\n{result.output[-4000:]}")

        starters += "starter" in confirmed
        references += "reference" in confirmed
        rejected = sum(label.startswith("partial-") for label in confirmed)
        partials += rejected
        if len(confirmed) == len(jobs):
            passed += 1
            print(f"PASS {case.name} | {count} files | starter rejected | reference passed | {rejected} partials rejected")

    print(f"{passed}/{len(cases)} cases passed")
    print(f"{starters}/{len(cases)} starters rejected after passing regressions")
    print(f"{references}/{len(cases)} references passed hidden acceptance")
    print(f"{partials}/{partial_total} partials passed public verification and failed hidden acceptance")
    print(f"Slowest hidden run {slowest:.3f}s")
    return int(not cases or passed != len(cases))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("solutions", type=Path)
    args = parser.parse_args()
    raise SystemExit(check_cases(args.solutions.resolve()))


if __name__ == "__main__":
    main()
