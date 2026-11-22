"""Check loader compatibility and isolation of the longhaul fixtures."""

import ast
import hashlib
import runpy
import shutil
import signal
from collections import Counter
from pathlib import Path

import pytest

from sca.eval.suites import archive_directory, load_suite, select_cases
from sca.kernel.acceptance import extract_acceptance
from sca.kinds.coding.spec import CodingSpec

SUITE = Path(__file__).resolve().parents[2] / "evals/opencode-evolve-longhaul"


def test_longhaul_loads_both_splits_with_coding_defaults() -> None:
    suite = load_suite(SUITE)
    assert suite["name"] == SUITE.name
    assert suite["kind"] == "coding"
    assert Counter(case["split"] for case in suite["cases"]) == {"dev": 11, "holdout": 5}
    assert len(select_cases(suite, "dev")) == 11
    assert len(select_cases(suite, "holdout")) == 5
    for case in suite["cases"]:
        spec = CodingSpec(repo_url="/fixture", **suite["defaults"], **case["spec"])
        assert spec.image == "python:3.12"
        assert spec.turn_timeout_s == 900
        assert spec.max_fix_attempts == 1
        assert spec.memory_enabled is False
        assert spec.verify_command == "python -B verify.py"
        assert case["acceptance_command"] == "python -B hidden.py"
        assert 4 <= sum(line.startswith("- ") for line in spec.instruction.splitlines()) <= 7


def protected_files(runner: Path) -> dict[str, str]:
    source = ast.parse(runner.read_text(encoding="utf-8"))
    return next(
        ast.literal_eval(statement.value)
        for statement in source.body
        if isinstance(statement, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "PROTECTED"
                for target in statement.targets)
    )


def test_starters_have_substantial_code_and_protected_legacy_contracts() -> None:
    for case in load_suite(SUITE)["cases"]:
        directory = Path(case["path"])
        repo = directory / "repo"
        files = {path.relative_to(repo).as_posix(): path
                 for path in repo.rglob("*") if path.is_file()}
        python = [path for name, path in files.items() if path.suffix == ".py" and name != "verify.py"]
        lines = sum(len(path.read_bytes().splitlines()) for path in python)
        assert 3_000 <= lines <= 8_000, (case["id"], lines)
        assert "hidden.py" not in files
        assert "acceptance" not in {path.relative_to(repo).parts[0] for path in files.values()}
        assert all("__pycache__" not in path.parts and path.suffix != ".pyc" for path in files.values())

        tests = sum(
            node.name.startswith("test_")
            for path in (repo / "tests").glob("test_*.py")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef)
        )
        assert tests >= 80, (case["id"], tests)
        protected = protected_files(directory / "acceptance/hidden.py")
        expected = {"README.md", "verify.py", "fixtures/workspace-v1.json", "tools/read_v1.py"}
        expected.update(name for name in files if name.startswith(("tests/", "docs/")))
        assert protected.keys() == expected, case["id"]
        for name, digest in protected.items():
            assert hashlib.sha256(files[name].read_bytes()).hexdigest() == digest, (case["id"], name)


def test_acceptance_archive_contains_only_the_hidden_runner(tmp_path: Path) -> None:
    for case in load_suite(SUITE)["cases"]:
        acceptance = Path(case["path"]) / "acceptance"
        destination = tmp_path / case["id"]
        destination.mkdir()
        extract_acceptance(archive_directory(acceptance), destination, 64_000)
        assert [path.name for path in destination.iterdir()] == ["hidden.py"]
        assert (destination / "hidden.py").read_bytes() == (acceptance / "hidden.py").read_bytes()


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="Long-haul acceptance uses a POSIX interval timer")
def test_acceptance_rejects_changes_to_public_verification(tmp_path: Path) -> None:
    checker = runpy.run_path(str(SUITE / "check_cases.py"))
    case = SUITE / "cases/purchase-rounds"
    repo = tmp_path / "repo"
    shutil.copytree(case / "repo", repo)
    (repo / "verify.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

    result = checker["check_repository"](repo, case / "acceptance")

    assert result.code == 1
    assert "AssertionError: verify.py" in result.output
    assert "Public verification passed." not in result.output
