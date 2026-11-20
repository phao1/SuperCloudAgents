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


def load_suite(directory: Path) -> dict:
    directory = directory.resolve()
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("suite symlinks are not allowed")
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0" + path.read_bytes())
    suite = yaml.safe_load((directory / "suite.yaml").read_text())
    cases = []
    for task in sorted((directory / "cases").glob("*/task.yaml")):
        case = task.parent
        acceptance = yaml.safe_load((case / "acceptance.yaml").read_text())
        spec = yaml.safe_load(task.read_text())
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
    return {**suite, "hash": digest.hexdigest(), "cases": cases}


def archive_directory(directory: Path) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(directory).as_posix(), recursive=False)
    return output.getvalue()
