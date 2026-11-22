"""Host filesystem and executable helpers for native services."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import BinaryIO


def lock_file(stream: BinaryIO, *, blocking: bool = True) -> None:
    if sys.platform != "win32":
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return

    import msvcrt

    # A byte-range lock can extend past EOF, so the same empty lock files work on both OSes.
    while True:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno != errno.EACCES:
                raise
            if not blocking:
                raise BlockingIOError(exc.errno, exc.strerror) from exc
            time.sleep(0.01)
        else:
            return


def unlock_file(stream: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream, fcntl.LOCK_UN)


def host_executable(name: str) -> Path | None:
    path = os.pathsep.join(entry for entry in os.get_exec_path() if Path(entry).is_absolute())
    if not path:
        return None
    found = shutil.which(name, path=path)
    return Path(found) if found and Path(found).is_absolute() else None


def windows_bash() -> Path:
    override = os.environ.get("SCA_BASH")
    if override:
        path = Path(override).expanduser()
        if path.is_absolute() and path.is_file():
            return path
        raise FileNotFoundError("SCA_BASH 必须指向 Git for Windows 的 bash.exe 绝对路径。")

    git = host_executable("git.exe")
    candidates = [parent / "bin" / "bash.exe" for parent in git.parents[1:3]] if git else []
    for name, directory in (("ProgramFiles", "Git"), ("LOCALAPPDATA", "Programs/Git")):
        if root := os.environ.get(name):
            candidates.append(Path(root) / directory / "bin" / "bash.exe")
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("找不到 Git Bash，请运行 winget install -e --id Git.Git，或设置 SCA_BASH。")


def remove_tree(directory: Path) -> None:
    if sys.platform != "win32":
        shutil.rmtree(directory)
        return

    def remove_readonly(function, name: str, error: BaseException) -> None:
        path = Path(name)
        if not isinstance(error, PermissionError) or path.is_symlink() or path.is_junction():
            raise error
        path.chmod(stat.S_IWRITE)
        function(name)

    shutil.rmtree(directory, onexc=remove_readonly)
