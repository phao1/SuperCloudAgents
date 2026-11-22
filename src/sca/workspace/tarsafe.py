"""Archive helpers. Sandbox archives always cross the data-filter boundary."""

from __future__ import annotations

import shutil
import sys
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO


def extract(archive: BinaryIO, target: Path, max_bytes: int | None = None) -> None:
    target.mkdir(parents=True, exist_ok=True)
    size = 0
    windows = sys.platform == "win32"
    links: list[tarfile.TarInfo] = []
    with tarfile.open(fileobj=archive, mode="r|*") as tar:
        for member in tar:
            size += member.size
            if max_bytes is not None and size > max_bytes:
                raise ValueError("Archive exceeds expanded size limit")
            # data_filter normalizes absolute paths; this contract rejects them outright.
            if PurePosixPath(member.name).is_absolute():
                raise tarfile.AbsolutePathError(member)
            if ".." in PurePosixPath(member.name).parts:
                raise tarfile.OutsideDestinationError(member, str(target))
            if windows:
                _check_windows_name(member, member.name)
                if member.issym() or member.islnk():
                    _check_windows_name(member, member.linkname)
                    tarfile.data_filter(member, str(target))
                    links.append(member)
                    continue
            tar.extract(member, target, filter="data")
    if links:
        _copy_links(target.resolve(), links, size, max_bytes)


def _check_windows_name(member: tarfile.TarInfo, name: str) -> None:
    if "\\" in name or any(":" in part or PureWindowsPath(part).is_reserved()
                            for part in PurePosixPath(name).parts):
        raise tarfile.OutsideDestinationError(member, name)


def _copy_links(
    target: Path, links: list[tarfile.TarInfo], size: int, max_bytes: int | None,
) -> None:
    # Windows can create ordinary files without Developer Mode, including for forward links.
    while links:
        pending = []
        for link in links:
            checked = tarfile.data_filter(link, str(target))
            destination = target / checked.name
            parent = destination.parent if checked.issym() else target
            source = (parent / checked.linkname).resolve()
            if not source.is_relative_to(target):
                raise tarfile.LinkOutsideDestinationError(link, str(source))
            if not source.exists():
                pending.append(link)
                continue
            if destination.exists() or destination.is_symlink() or destination.is_junction():
                raise tarfile.OutsideDestinationError(link, str(destination))
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                if destination.resolve().is_relative_to(source):
                    raise tarfile.LinkOutsideDestinationError(link, str(source))
                for child in source.rglob("*"):
                    if child.is_symlink() or child.is_junction():
                        raise tarfile.LinkOutsideDestinationError(link, str(child))
                    if child.is_file():
                        size += child.stat().st_size
                copy = shutil.copytree
            else:
                size += source.stat().st_size
                copy = shutil.copyfile
            if max_bytes is not None and size > max_bytes:
                raise ValueError("Archive exceeds expanded size limit")
            copy(source, destination)
        if len(pending) == len(links):
            raise tarfile.ExtractError("Archive link target is missing or circular")
        links = pending


def pack(
    source: Path, output: BinaryIO, exclude: frozenset[str] = frozenset(), *, name: str = ".",
) -> None:
    """Tar `source`; top-level entries named in `exclude` are left out."""

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        relative = PurePosixPath(info.name).relative_to(name)
        if sys.platform == "win32" and (source / relative).is_junction():
            return None
        top = next(iter(relative.parts), ".")
        return None if top in exclude else info

    with tarfile.open(fileobj=output, mode="w|") as tar:
        tar.add(source, arcname=name, filter=keep)


async def chunks(source: BinaryIO) -> AsyncIterator[bytes]:
    while chunk := source.read(64 * 1024):
        yield chunk


async def receive(archive: AsyncIterator[bytes], output: BinaryIO) -> None:
    async for chunk in archive:
        output.write(chunk)
    output.seek(0)
