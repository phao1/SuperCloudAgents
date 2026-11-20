"""Check the real evolution suite's splits, fixture boundaries, and legacy coverage."""

import ast
import hashlib
from collections import Counter
from pathlib import Path

from sca.eval.suites import archive_directory, load_suite, select_cases
from sca.kernel.acceptance import extract_acceptance
from sca.kinds.coding.spec import CodingSpec

SUITE = Path(__file__).resolve().parents[2] / "evals/opencode-evolve-real"
BUILD_CATEGORIES = {"language-build", "data-build", "stateful-build"}
CATEGORIES = BUILD_CATEGORIES | {
    "scattered-spec", "vertical-feature", "pipeline-debug", "state-debug", "summary-verification",
}


def test_evolve_real_suite_splits_and_defaults() -> None:
    suite = load_suite(SUITE)
    cases = suite["cases"]
    assert suite["name"] == SUITE.name
    assert suite["kind"] == "coding"
    assert len(cases) == len(list((SUITE / "cases").iterdir())) == 24
    assert len(select_cases(suite, "dev")) == 16
    assert len(select_cases(suite, "holdout")) == 8
    assert {case["category"] for case in cases} == CATEGORIES
    for category in CATEGORIES:
        assert Counter(case["split"] for case in cases if case["category"] == category) == {
            "dev": 2, "holdout": 1,
        }

    for case in cases:
        spec = CodingSpec(repo_url="/fixture", **suite["defaults"], **case["spec"])
        assert spec.image == "python:3.12"
        assert spec.turn_timeout_s == 900
        assert spec.max_fix_attempts == 1
        assert spec.memory_enabled is False
        assert spec.verify_command == "python -B verify.py"
        assert case["acceptance_command"] == "python -B hidden.py"


def test_evolve_real_suite_protects_the_published_contracts() -> None:
    for case in load_suite(SUITE)["cases"]:
        path = Path(case["path"])
        repo = path / "repo"
        acceptance = path / "acceptance/hidden.py"
        assert not (repo / "hidden.py").exists()
        assert not (repo / "acceptance").exists()
        tree = ast.parse(acceptance.read_text(encoding="utf-8"))
        protected = next(
            ast.literal_eval(statement.value)
            for statement in tree.body
            if isinstance(statement, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "PROTECTED"
                    for target in statement.targets)
        )
        expected = {"verify.py"}
        if case["category"] in BUILD_CATEGORIES:
            assert case["spec"]["instruction"].strip().endswith("as specified in SPEC.md.")
            expected.add("SPEC.md")
        else:
            expected.update(file.relative_to(repo).as_posix()
                            for file in (repo / "docs").rglob("*.md"))
        if case["category"] == "vertical-feature":
            expected.update({"README.md", "stockroom/schema.sql"})
            expected.update(file.relative_to(repo).as_posix() for file in (repo / "tests").glob("*.py"))
        assert protected.keys() == expected, case["id"]
        for name, digest in protected.items():
            assert hashlib.sha256((repo / name).read_bytes()).hexdigest() == digest, (case["id"], name)


def test_vertical_fixtures_include_existing_code_and_regressions() -> None:
    cases = [case for case in load_suite(SUITE)["cases"] if case["category"] == "vertical-feature"]
    for case in cases:
        repo = Path(case["path"]) / "repo"
        files = [file for file in repo.rglob("*") if file.is_file()]
        assert 20 <= len(files) <= 60
        python_lines = sum(len(file.read_bytes().splitlines()) for file in files if file.suffix == ".py")
        assert 3000 <= python_lines <= 8000
        tests = sum(
            node.name.startswith("test_")
            for file in (repo / "tests").glob("test_*.py")
            for node in ast.walk(ast.parse(file.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef)
        )
        assert 40 <= tests <= 150


def test_real_acceptance_archive_extracts_only_the_hidden_runner(tmp_path: Path) -> None:
    source = SUITE / "cases/stock-reorder/acceptance"
    archive = archive_directory(source)
    extract_acceptance(archive, tmp_path, 64_000)
    assert list(tmp_path.iterdir()) == [tmp_path / "hidden.py"]
    assert (tmp_path / "hidden.py").read_bytes() == (source / "hidden.py").read_bytes()
