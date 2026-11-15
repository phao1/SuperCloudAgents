"""Bounded hidden-test archive extraction into a disposable clean checkout."""

import io
import tarfile
from pathlib import Path, PurePosixPath


def extract_acceptance(data: bytes, target: Path, limit: int) -> None:
    size = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r|*") as archive:
        for count, member in enumerate(archive, 1):
            path = PurePosixPath(member.name)
            size += member.size
            if (count > 4096 or size > limit or member.size < 0 or path.is_absolute()
                    or ".." in path.parts or ".git" in path.parts
                    or not (member.isfile() or member.isdir())):
                raise ValueError("unsafe or oversized acceptance archive")
            archive.extract(member, target, filter="data")
