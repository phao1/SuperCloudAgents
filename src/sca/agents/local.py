"""Agent executables available to a source-mode worker."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from sca.config import Settings
from sca.host import host_executable

SOURCE_BACKENDS = ("opencode", "codex", "pi", "dsh", "opencode-brain")
DSH_PACKAGE = "@deepseek-ai/dsh"


def dsh_install_dir(settings: Settings) -> Path:
    return settings.data_dir.resolve() / "tools" / "dsh"


def _package_manifest(package: Path) -> dict:
    try:
        metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _node_entry(package: Path, name: str, manifest: dict) -> Path | None:
    entry = manifest.get("bin")
    if isinstance(entry, dict):
        entry = entry.get(name)
    if not isinstance(entry, str):
        return None
    binary = (package / entry).resolve()
    return binary if binary.is_relative_to(package.resolve()) and binary.is_file() else None


def installed_dsh(settings: Settings) -> Path | None:
    root = dsh_install_dir(settings) / "node_modules"
    package = root / DSH_PACKAGE
    manifest = _package_manifest(package)
    if manifest.get("version") != settings.dsh_version:
        return None
    if sys.platform == "win32":
        return _node_entry(package, "dsh", manifest)
    binary = root / ".bin" / "dsh"
    return binary if binary.is_file() and os.access(binary, os.X_OK) else None


def host_agent_binary(settings: Settings, name: str) -> Path | None:
    executable = f"{name}.exe" if sys.platform == "win32" else name
    if name == "dsh":
        if host_executable("node.exe" if sys.platform == "win32" else "node") is None:
            return None
        return installed_dsh(settings)
    if name == "opencode":
        binary = settings.opencode_versions_dir / settings.opencode_default_version / executable
        if binary.is_symlink() or binary.parent.is_symlink():
            return None
    elif name == "codex" and settings.codex_bin_dir:
        binary = settings.codex_bin_dir.expanduser() / executable
    elif name in ("codex", "pi"):
        if sys.platform == "win32" and name == "pi":
            if binary := host_executable(executable):
                return binary
            if (npm := host_executable("pi.cmd")) and host_executable("node.exe"):
                package = npm.parent / "node_modules" / "@earendil-works" / "pi-coding-agent"
                return _node_entry(package, "pi", _package_manifest(package))
            return None
        return host_executable(executable)
    else:
        return None
    return binary.absolute() if binary.is_file() and os.access(binary, os.X_OK) else None


def unavailable_source_backend(name: str) -> str:
    if name == "opencode-brain":
        return "OpenCode 脑手分离需要 Docker 模式"
    if name == "dsh":
        return "本机未安装 DeepSeek Harness 或 Node.js，请运行源码模式启动脚本"
    return f"本机未安装可执行的 {name}，此后端在源码模式下不可用"
