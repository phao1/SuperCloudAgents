"""Load reviewed local suites and prepare API uploads."""

import hashlib
import io
import tarfile
from pathlib import Path

import yaml


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
        cases.append({"id": case.name, "path": str(case),
                      "spec": yaml.safe_load(task.read_text()),
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
