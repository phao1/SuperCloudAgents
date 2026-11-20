"""Load reviewed local suites and prepare API uploads."""

import hashlib
import io
import tarfile
from pathlib import Path
from typing import Literal

import yaml

from sca.eval.trajectory import validate_checks

Split = Literal["dev", "holdout"]


def select_cases(suite: dict, split: Split | None = None) -> list[dict]:
    if split not in (None, "dev", "holdout"):
        raise ValueError("split must be dev or holdout")
    cases = [case for case in suite["cases"] if split is None or case.get("split") == split]
    if not cases:
        raise ValueError("suite has no cases for requested split")
    return cases


def case_metadata(cases: list[dict]) -> dict[str, dict]:
    return {case["id"]: {key: case[key] for key in ("split", "category") if key in case}
            for case in cases}


def matches_suite_hash(suite: dict, expected: str) -> bool:
    return expected in (suite["hash"], suite.get("legacy_hash"))


def _read_mapping(path: Path) -> dict:
    try:
        value = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        raise ValueError(f"invalid YAML in {path.name}") from None
    if isinstance(value, dict):
        return value
    raise ValueError(f"{path.name} must be a mapping")


def load_suite(directory: Path) -> dict:
    directory = directory.resolve()
    digest = hashlib.sha256()
    legacy_digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("suite symlinks are not allowed")
        if path.is_file():
            relative = path.relative_to(directory).as_posix()
            try:
                entry = relative.encode() + b"\0" + path.read_bytes()
            except OSError:
                if relative != "README.md":
                    raise
                # A concurrent docs edit can invalidate the legacy digest without changing the suite.
                legacy_digest = None
                continue
            if legacy_digest is not None:
                legacy_digest.update(entry)
            # READMEs within case fixtures are part of the evaluated inputs.
            if relative != "README.md":
                digest.update(entry)
    suite = _read_mapping(directory / "suite.yaml")
    if "name" not in suite or not isinstance(suite.get("defaults", {}), dict):
        raise ValueError("suite needs a name and mapping defaults")
    cases = []
    for task in sorted((directory / "cases").glob("*/task.yaml")):
        case = task.parent
        acceptance = _read_mapping(case / "acceptance.yaml")
        if "command" not in acceptance:
            raise ValueError("acceptance command is required")
        spec = _read_mapping(task)
        metadata = {key: spec.pop(key) for key in ("split", "category") if key in spec}
        if "split" in metadata and metadata["split"] not in ("dev", "holdout"):
            raise ValueError(f"invalid split in {case.name}")
        if "category" in metadata and (not isinstance(metadata["category"], str)
                                       or not metadata["category"].strip()):
            raise ValueError(f"invalid category in {case.name}")
        cases.append({"id": case.name, "path": str(case), **metadata,
                      "spec": spec,
                      "trajectory_checks": validate_checks(acceptance.get("trajectory_checks", suite.get("trajectory_checks", []))),
                      "acceptance_command": acceptance["command"]})
    if not cases:
        raise ValueError("suite has no cases")
    return {**suite, "hash": digest.hexdigest(),
            "legacy_hash": legacy_digest.hexdigest() if legacy_digest is not None else None,
            "cases": cases}


def archive_directory(directory: Path) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(directory).as_posix(), recursive=False)
    return output.getvalue()
