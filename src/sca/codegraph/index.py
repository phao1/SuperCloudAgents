"""Build and query a repository's file graph and symbol table."""

from __future__ import annotations

import os
import posixpath
import re
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator, Mapping
from importlib.util import resolve_name
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from sca.codegraph.parsers import DEFAULT_PARSERS, JS_SUFFIXES, ModuleImport, SourceParser

MAX_SOURCE_BYTES = 1024 * 1024
_SKIP_DIRS = {".git", ".data", ".venv", "__pycache__", "node_modules"}


class FileSymbols(BaseModel):
    defined: list[str]
    referenced: list[str]
    signatures: list[str]


class CodeIndex(BaseModel):
    files: list[str]
    dependencies: dict[str, list[str]]
    symbols: dict[str, FileSymbols]
    test_sources: dict[str, list[str]]


class Impact(BaseModel):
    changed_paths: list[str]
    impacted_files: list[str]
    tests_to_run: list[str]
    base_sha: str | None = Field(default=None)
    commit: str | None = Field(default=None)


def _python_module(path: str) -> str:
    parts = PurePosixPath(path).with_suffix("").parts
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _qualified(name: str, package: str) -> str:
    if name.startswith("."):
        try:
            return resolve_name(name, package)
        except ImportError:
            return ""
    return name


def _python_package(path: str) -> str:
    module = _python_module(path)
    return module if PurePosixPath(path).name == "__init__.py" else module.rpartition(".")[0]


def _python_imports(
    path: str, imports: Iterable[ModuleImport], modules: Mapping[str, str],
) -> set[str]:
    package = _python_package(path)
    deps = set()
    for imp in imports:
        target = _qualified(imp.module, package)
        if not target:
            continue
        parts = target.split(".")
        for stop in range(1, len(parts) + 1):
            name = ".".join(parts[:stop])
            if name in modules:
                deps.add(modules[name])
        deps.update(modules[f"{target}.{name}"] for name in imp.names if f"{target}.{name}" in modules)
    return deps


def _js_target(path: str, target: str, files: set[str]) -> str | None:
    if target not in {".", ".."} and not target.startswith(("./", "../")):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    if base == ".." or base.startswith("../"):
        return None
    if base in files:
        return base
    stem, suffix = posixpath.splitext(base)
    ts_suffixes = {".js": (".ts", ".tsx"), ".mjs": (".mts",), ".cjs": (".cts",)}
    candidates = (
        *(stem + ext for ext in ts_suffixes.get(suffix, ())),
        *(base + ext for ext in JS_SUFFIXES),
        *(posixpath.join(base, "index" + ext) for ext in JS_SUFFIXES),
    )
    return next((candidate for candidate in candidates if candidate in files), None)


def _test_stem(path: str) -> str | None:
    file = PurePosixPath(path)
    if file.suffix == ".py":
        if file.stem.startswith("test_"):
            return file.stem.removeprefix("test_")
        if file.stem.endswith("_test"):
            return file.stem.removesuffix("_test")
    if file.suffix in JS_SUFFIXES:
        if file.stem.endswith((".test", ".spec")):
            return file.stem.rsplit(".", 1)[0]
        if "__tests__" in file.parts[:-1]:
            return file.stem
    return None


def _map_tests(
    files: set[str], dependencies: dict[str, list[str]],
) -> dict[str, list[str]]:
    stems = defaultdict(set)
    tests = {}
    for path in sorted(files):
        stem = _test_stem(path)
        if stem is None:
            suffix = PurePosixPath(path).suffix
            stems[suffix == ".py", PurePosixPath(path).stem].add(path)
        else:
            tests[path] = stem

    result = {}
    for path, stem in tests.items():
        test_file = PurePosixPath(path)
        sources = set(dependencies.get(path, ())) - tests.keys()
        sources.update(stems[test_file.suffix == ".py", stem])
        if test_file.suffix == ".py":
            sources.update(
                conftest for parent in test_file.parents
                if (conftest := (parent / "conftest.py").as_posix()) in files
            )
        result[path] = sorted(sources)
    return result


def build_index(
    sources: Iterable[tuple[str, bytes | None]],
    parsers: Mapping[str, SourceParser] = DEFAULT_PARSERS,
) -> CodeIndex:
    """Index repo-relative files. A missing body still contributes a file path."""
    files = set()
    parsed = {}
    for path, source in sources:
        files.add(path)
        parser = parsers.get(PurePosixPath(path).suffix)
        if source is None or parser is None:
            continue
        try:
            parsed[path] = parser.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            continue

    modules = {}
    for path in sorted(files):
        if path.endswith(".py"):
            module = _python_module(path)
            if module:
                modules.setdefault(module, path)

    deps, symbols = {}, {}
    for path, data in sorted(parsed.items()):
        references = set(data.referenced)
        if path.endswith(".py"):
            package = _python_package(path)
            references = {
                resolved for name in references if (resolved := _qualified(name, package))
            }
            edges = _python_imports(path, data.imports, modules)
        else:
            edges = set()
            for imp in data.imports:
                target = _js_target(path, imp.module, files)
                if target is not None:
                    edges.add(target)
                references.update(f"{target or imp.module}:{name}" for name in imp.names)
        deps[path] = sorted(edges - {path})
        symbols[path] = FileSymbols(
            defined=sorted(data.defined), referenced=sorted(references),
            signatures=data.signatures,
        )
    code_files = {path for path in files if PurePosixPath(path).suffix in parsers}
    return CodeIndex(files=sorted(files), dependencies=deps, symbols=symbols,
                     test_sources=_map_tests(code_files, deps))


def index_directory(root: Path, parsers: Mapping[str, SourceParser] = DEFAULT_PARSERS) -> CodeIndex:
    def sources() -> Iterator[tuple[str, bytes | None]]:
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if name not in _SKIP_DIRS]
            for name in files:
                path = Path(directory) / name
                source = None
                if path.suffix in parsers and not path.is_symlink() and path.is_file():
                    with path.open("rb") as file:
                        source = file.read(MAX_SOURCE_BYTES + 1)
                    if len(source) > MAX_SOURCE_BYTES:
                        source = None
                yield path.relative_to(root).as_posix(), source

    return build_index(sources(), parsers)


def _symbol_matches(path: str, table: FileSymbols, queries: set[str]) -> bool:
    module = _python_module(path) if path.endswith(".py") else ""
    for name in table.defined:
        if (name in queries or f"{path}:{name}" in queries
                or (module and f"{module}.{name}" in queries)):
            return True
    for name in table.referenced:
        short_name = re.split(r"[.:]", name)[-1]
        if name in queries or short_name in queries:
            return True
    return False


def analyze_impact(
    *indexes: CodeIndex,
    changed_paths: Iterable[str] = (),
    changed_symbols: Iterable[str] = (),
) -> Impact:
    changed = set(changed_paths)
    affected = set(changed)
    queries = set(changed_symbols)
    reverse = defaultdict(set)
    tests = set()
    for index in indexes:
        for mapping in (index.dependencies, index.test_sources):
            for consumer, dependencies in mapping.items():
                for dependency in dependencies:
                    reverse[dependency].add(consumer)
        tests.update(index.test_sources)
        if queries:
            affected.update(path for path, table in index.symbols.items()
                            if _symbol_matches(path, table, queries))

    queue = deque(affected)
    while queue:
        for dependent in reverse[queue.popleft()] - affected:
            affected.add(dependent)
            queue.append(dependent)
    return Impact(
        changed_paths=sorted(changed), impacted_files=sorted(affected),
        tests_to_run=sorted(affected & tests),
    )
