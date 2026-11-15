"""Archive helpers. Sandbox archives always cross the data-filter boundary."""

from __future__ import annotations

import tarfile
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import BinaryIO


def extract(archive: BinaryIO, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=archive, mode="r|*") as tar:
        for member in tar:
            # data_filter normalizes absolute paths; this contract rejects them outright.
            if PurePosixPath(member.name).is_absolute():
                raise tarfile.AbsolutePathError(member)
            if ".." in PurePosixPath(member.name).parts:
                raise tarfile.OutsideDestinationError(member, str(target))
            tar.extract(member, target, filter="data")


def pack(source: Path, output: BinaryIO, exclude: frozenset[str] = frozenset()) -> None:
    """Tar `source`; top-level entries named in `exclude` are left out."""

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        top = info.name.removeprefix("./").split("/", 1)[0]
        return None if top in exclude else info

    with tarfile.open(fileobj=output, mode="w|") as tar:
        tar.add(source, arcname=".", filter=keep)


async def chunks(source: BinaryIO) -> AsyncIterator[bytes]:
    while chunk := source.read(64 * 1024):
        yield chunk


async def receive(archive: AsyncIterator[bytes], output: BinaryIO) -> None:
    async for chunk in archive:
        output.write(chunk)
    output.seek(0)
