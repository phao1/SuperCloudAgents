import asyncio
from pathlib import Path

import pytest

from sca.codegraph.index import analyze_impact, index_directory
from sca.codegraph.repository import analyze_patch, index_commit
from sca.kinds.coding.planning import planner_context
from sca.kinds.coding.subtasks import contract_hints
from sca.workspace.git import GitWorkspace


@pytest.fixture(autouse=True)
def local_io(monkeypatch):
    async def to_thread(function, /, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", to_thread)


def write_files(root: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def test_python_imports_symbols_impact_and_contracts(tmp_path):
    write_files(tmp_path, {
        "src/pkg/__init__.py": "from .numbers import total",
        "src/pkg/numbers.py": (
            "LIMIT: int = 2\n"
            "async def total(x: int, /, *, limit: int = LIMIT) -> int:\n"
            "    return x + LIMIT\n"
            "def _private(): pass\n"
        ),
        "src/pkg/service.py": "from . import numbers as n\nwork = n.total\n",
        "src/pkg/report.py": "from pkg.numbers import total as add\nentry = add\n",
        "tests/test_api.py": "from pkg.service import work\n",
        "tests/test_numbers.py": "# Mapped by file name\n",
        "tests/test_unrelated.py": "pass\n",
        "tests/conftest.py": "pass\n",
    })
    index = index_directory(tmp_path)
    assert index.dependencies["src/pkg/service.py"] == [
        "src/pkg/__init__.py", "src/pkg/numbers.py",
    ]
    assert index.symbols["src/pkg/numbers.py"].defined == ["LIMIT", "_private", "total"]
    assert index.symbols["src/pkg/numbers.py"].referenced == ["LIMIT"]
    assert "pkg.numbers.total" in index.symbols["src/pkg/service.py"].referenced
    assert "pkg.numbers.total" in index.symbols["src/pkg/report.py"].referenced
    result = analyze_impact(index, changed_symbols=["pkg.numbers.total"])
    assert result.impacted_files == [
        "src/pkg/__init__.py", "src/pkg/numbers.py", "src/pkg/report.py", "src/pkg/service.py",
        "tests/test_api.py", "tests/test_numbers.py",
    ]
    assert result.tests_to_run == ["tests/test_api.py", "tests/test_numbers.py"]
    assert len(analyze_impact(index, changed_paths=["tests/conftest.py"]).tests_to_run) == 3
    summary = planner_context(index, "调整 `pkg.numbers.total` 和 `src/pkg/numbers.py`。")
    assert "Repository map" in summary and "Candidate tests" in summary
    assert "tests/test_api.py" in summary and len(summary) <= 8000
    hints = contract_hints(index, {
        "service": ["src/pkg/service.py"], "report": ["src/pkg/report.py"],
    }, [])
    assert "contracts you must not break" in hints
    assert "async def total(x: int, /, *, limit: int=...) -> int" in hints
    assert "_private" not in hints and "LIMIT" not in hints


def test_js_exports_relative_imports_and_test_mapping(tmp_path):
    write_files(tmp_path, {
        "src/lib/index.ts": "export function greet() {}\nexport default 1;\n",
        "src/app.ts": "import { greet as hello } from './lib/index.js';\nexport const x = hello;",
        "src/barrel.ts": "export { greet as hello } from './lib'\n",
        "tests/app.spec.ts": "import '../src/app';\n// import '../src/unused';\n",
        "tests/__tests__/unused.ts": "export const fake = `\nimport '../../src/app'\n`;\n",
        "src/unused.ts": "export const y = 1;",
    })
    index = index_directory(tmp_path)
    assert index.dependencies["src/app.ts"] == ["src/lib/index.ts"]
    assert index.dependencies["src/barrel.ts"] == ["src/lib/index.ts"]
    assert index.dependencies["tests/app.spec.ts"] == ["src/app.ts"]
    assert index.dependencies["tests/__tests__/unused.ts"] == []
    assert index.symbols["src/lib/index.ts"].defined == ["default", "greet"]
    assert index.symbols["src/app.ts"].referenced == ["src/lib/index.ts:greet"]
    assert index.symbols["src/barrel.ts"].defined == ["hello"]
    impact = analyze_impact(index, changed_symbols=["src/lib/index.ts:greet"])
    assert impact.tests_to_run == ["tests/app.spec.ts"]
    assert analyze_impact(index, changed_paths=["src/unused.ts"]).tests_to_run == [
        "tests/__tests__/unused.ts",
    ]


async def test_commit_cache_and_patch_union(tmp_path):
    source = GitWorkspace(tmp_path / "source", "main")
    source.task_dir.mkdir()
    await source._git("init", "-b", "main")
    write_files(source.task_dir, {"util.py": "def old(): pass\n",
                                  "tests/test_old.py": "from util import old",
                                  "notes.txt": "before\n"})
    await source._git("add", ".")
    await source._git(
        "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "base"
    )
    base = (await source._git("rev-parse", "HEAD")).strip()
    await source._git("mv", "util.py", "renamed.py")
    write_files(source.task_dir, {"tests/test_added.py": "from renamed import old"})
    (source.task_dir / "linked.py").symlink_to(tmp_path / "outside.py")
    (tmp_path / "outside.py").write_text("def outside(): pass\n")
    await source._git("add", ".")
    await source._git(
        "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-m", "change"
    )
    ws = GitWorkspace(tmp_path / "task", "sca/test")
    head = await ws.prepare(str(source.task_dir), "main", mirror_only=True)
    layers = tmp_path / "layers"
    impacted = await analyze_patch(ws.mirror, base, head.base_sha, layers, "tenant")
    assert impacted.changed_paths == ["linked.py", "renamed.py", "tests/test_added.py", "util.py"]
    assert impacted.tests_to_run == ["tests/test_added.py", "tests/test_old.py"]
    index = await index_commit(ws.mirror, head.base_sha, layers, "tenant")
    assert "linked.py" in index.files and "linked.py" not in index.symbols
    assert len(list(layers.rglob("*.json"))) == 2
    assert await index_commit(tmp_path / "missing.git", head.base_sha, layers, "tenant") == index
    await index_commit(ws.mirror, head.base_sha, layers, "other")
    assert len(list(layers.rglob(f"{head.base_sha}.json"))) == 2
