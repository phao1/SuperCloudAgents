"""Agent executables available to a source-mode worker."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from sca.config import Settings

SOURCE_BACKENDS = ("opencode", "codex", "pi", "opencode-brain")


def host_agent_binary(settings: Settings, name: str) -> Path | None:
    if name == "opencode":
        binary = settings.opencode_versions_dir / settings.opencode_default_version / "opencode"
        if binary.is_symlink() or binary.parent.is_symlink():
            return None
    elif name == "codex" and settings.codex_bin_dir:
        binary = settings.codex_bin_dir.expanduser() / "codex"
    elif name in ("codex", "pi"):
        found = shutil.which(name, path=os.pathsep.join(
            entry for entry in os.get_exec_path() if Path(entry).is_absolute()
        ))
        if not found:
            return None
        binary = Path(found)
    else:
        return None
    return binary.absolute() if binary.is_file() and os.access(binary, os.X_OK) else None


def unavailable_source_backend(name: str) -> str:
    if name == "opencode-brain":
        return "OpenCode 脑手分离需要 Docker 模式"
    return f"本机未安装可执行的 {name}，此后端在源码模式下不可用"
