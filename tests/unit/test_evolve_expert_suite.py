"""Exercise the expert suite's loader, fixture boundaries, and offline checker."""

import ast
import hashlib
import runpy
from collections import Counter
from pathlib import Path

import pytest

from sca.eval.suites import archive_directory, load_suite, select_cases
from sca.kernel.acceptance import extract_acceptance
from sca.kinds.coding.spec import CodingSpec

SUITE = Path(__file__).resolve().parents[2] / "evals/opencode-evolve-expert"


def test_expert_suite_loads_both_splits_with_coding_defaults() -> None:
    suite = load_suite(SUITE)
    assert suite["name"] == SUITE.name
    assert suite["kind"] == "coding"
    assert Counter(case["split"] for case in suite["cases"]) == {"dev": 14, "holdout": 6}
    assert len(select_cases(suite, "dev")) == 14
    assert len(select_cases(suite, "holdout")) == 6
    assert len({case["id"] for case in suite["cases"]}) == 20
    for case in suite["cases"]:
        spec = CodingSpec(repo_url="/fixture", **suite["defaults"], **case["spec"])
        assert spec.image == "python:3.12"
        assert spec.turn_timeout_s == 900
        assert spec.max_fix_attempts == 1
        assert spec.memory_enabled is False
        assert spec.verify_command == "python -B verify.py"
        assert case["acceptance_command"] == "python -B hidden.py"


def protected_files(hidden: Path) -> dict[str, str]:
    tree = ast.parse(hidden.read_text(encoding="utf-8"))
    return next(
        ast.literal_eval(statement.value)
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "PROTECTED"
                for target in statement.targets)
    )


def test_expert_cases_have_protected_contracts_and_substantial_regressions() -> None:
    for case in load_suite(SUITE)["cases"]:
        directory = Path(case["path"])
        repo = directory / "repo"
        files = {file.relative_to(repo).as_posix(): file for file in repo.rglob("*")
                 if file.is_file()}
        assert len(files) >= 15, case["id"]
        assert not {"hidden.py", "acceptance/hidden.py"} & files.keys()
        assert all("__pycache__" not in file.parts and file.suffix != ".pyc"
                   for file in files.values()), case["id"]
        python_lines = sum(len(file.read_bytes().splitlines()) for file in files.values()
                           if file.suffix == ".py" and file.name != "verify.py")
        assert 1500 <= python_lines <= 6000, (case["id"], python_lines)
        regressions = sum(
            node.name.startswith("test_")
            for file in (repo / "tests").glob("test_*.py")
            for node in ast.walk(ast.parse(file.read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef)
        )
        assert regressions >= 50, (case["id"], regressions)

        expected = {"README.md", "verify.py"}
        expected.update(name for name in files if name.startswith(("docs/", "tests/")))
        if "stockroom/schema.sql" in files:
            expected.add("stockroom/schema.sql")
        protected = protected_files(directory / "acceptance/hidden.py")
        assert protected.keys() == expected, case["id"]
        for name, digest in protected.items():
            assert hashlib.sha256(files[name].read_bytes()).hexdigest() == digest, (case["id"], name)


def test_expert_acceptance_archive_contains_only_the_hidden_runner(tmp_path: Path) -> None:
    source = SUITE / "cases/worker-claim-fence/acceptance"
    extract_acceptance(archive_directory(source), tmp_path, 64_000)
    assert list(tmp_path.iterdir()) == [tmp_path / "hidden.py"]
    assert (tmp_path / "hidden.py").read_bytes() == (source / "hidden.py").read_bytes()


@pytest.fixture
def checker() -> dict:
    # run_path loads the checker without writing a cache into the hashed suite.
    return runpy.run_path(str(SUITE / "check_cases.py"))


@pytest.fixture
def local_case(tmp_path: Path) -> tuple[Path, Path, Path]:
    suite, solutions = tmp_path / "suite", tmp_path / "solutions"
    case = suite / "cases/example"
    (case / "repo").mkdir(parents=True)
    (case / "task.yaml").touch()
    reference = solutions / case.name
    for repository, mode in (
        (case / "repo", "starter"),
        (reference, "reference"),
        (reference / "shallow-first", "shallow"),
        (reference / "shallow-second", "shallow"),
    ):
        repository.mkdir(parents=True, exist_ok=True)
        (repository / "mode").write_text(mode, encoding="utf-8")
    (case / "acceptance").mkdir()
    (case / "acceptance/hidden.py").write_text(
        """from pathlib import Path

path = Path("mode")
mode = path.read_text()
path.write_text("modified copy")
print("Legacy regression suite passed.")
if mode == "starter":
    print("Public verification failed.")
    raise SystemExit(1)
print("Public verification passed.")
if mode == "reference":
    print("Feature checks passed.")
    raise SystemExit(0)
if mode == "shallow":
    print("Feature assertion failed.")
raise SystemExit(1)
""",
        encoding="utf-8",
    )
    return suite, solutions, case


def test_checker_validates_all_four_repositories_in_isolated_copies(
    checker: dict, local_case: tuple[Path, Path, Path], capsys: pytest.CaptureFixture,
) -> None:
    suite, solutions, case = local_case
    reference = solutions / case.name
    (reference / "shallow-first/untouched.pyc").write_bytes(b"cache")
    assert checker["check_cases"](solutions, suite=suite) == 0
    assert "2/2 shallow fixes passed public verification and failed hidden acceptance" in capsys.readouterr().out
    assert (reference / "mode").read_text(encoding="utf-8") == "reference"
    assert (case / "repo/mode").read_text(encoding="utf-8") == "starter"
    assert not (case / "repo/hidden.py").exists()


@pytest.mark.parametrize("mode", ["starter", "reference", "crash"])
def test_checker_rejects_shallow_fix_with_wrong_verification_stage(
    checker: dict, local_case: tuple[Path, Path, Path], mode: str,
    capsys: pytest.CaptureFixture,
) -> None:
    suite, solutions, case = local_case
    (solutions / case.name / "shallow-first/mode").write_text(mode, encoding="utf-8")
    assert checker["check_cases"](solutions, suite=suite) == 1
    assert "FAIL example/shallow-first" in capsys.readouterr().out


def test_checker_requires_two_shallow_fixes(
    checker: dict, local_case: tuple[Path, Path, Path], capsys: pytest.CaptureFixture,
) -> None:
    suite, solutions, case = local_case
    (solutions / case.name / "shallow-first").rename(solutions / "unavailable")
    assert checker["check_cases"](solutions, suite=suite) == 1
    assert "expected at least two shallow fixes, found 1" in capsys.readouterr().out


def test_checker_does_not_count_a_syntax_error_as_an_expected_rejection(
    checker: dict, local_case: tuple[Path, Path, Path], capsys: pytest.CaptureFixture,
) -> None:
    suite, solutions, case = local_case
    (case / "repo/broken.py").write_text("def incomplete(", encoding="utf-8")
    assert checker["check_cases"](solutions, suite=suite) == 1
    output = capsys.readouterr().out
    assert "FAIL example/starter" in output
    assert "0/1 starters rejected" in output


def test_hidden_runner_rejects_modified_public_verifier(
    checker: dict, tmp_path: Path,
) -> None:
    from shutil import copytree

    case = SUITE / "cases/worker-claim-fence"
    repo = tmp_path / "repo"
    copytree(case / "repo", repo)
    (repo / "verify.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    result = checker["check_repository"](repo, case / "acceptance")
    assert result.code == 1
    assert "AssertionError: verify.py" in result.output
    assert "Public verification passed." not in result.output
