"""Prepare Linux Node agent runtimes outside task sandboxes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import suppress
from math import ceil
from pathlib import Path
from typing import Literal
from uuid import uuid4

from sca.config import Settings
from sca.core.errors import SandboxError
from sca.core.models import Mount
from sca.host import lock_file
from sca.sandbox._exec import NODE_RUNTIME_DIR, NODE_RUNTIME_LABEL

NODE_VERSION = "24.22.3"
NODE_IMAGE = f"node:{NODE_VERSION}-bookworm-slim"
BYTES_PER_MB = 1_000_000
INSTALL_HEADROOM_MB = 1000
PACKAGES = {"dsh": "@deepseek-ai/dsh", "pi": "@earendil-works/pi-coding-agent"}

INSTALL = r"""
if [ "$1" = copy ]; then
    mkdir -p node/bin
    cp /usr/local/bin/node node/bin/node
    if [ -f /usr/local/LICENSE ]; then cp /usr/local/LICENSE node/LICENSE; fi
fi
mkdir -p agent
npm install --prefix agent --omit=dev --no-audit --no-fund --save-exact "$2"
# Source/declaration maps and TypeScript declarations are irrelevant to the CLI.
find agent/node_modules -type f \( -name '*.js.map' -o -name '*.mjs.map' -o -name '*.cjs.map' \
    -o -name '*.d.ts' -o -name '*.d.ts.map' \) -delete
if [ "$3" = dsh ]; then
    DSH_HOME=/tmp/sca-dsh DSH_TELEMETRY_MODE=DISABLED node agent/node_modules/.bin/dsh --profile headless --help
else
    PI_OFFLINE=1 PI_TELEMETRY=0 node agent/node_modules/.bin/pi --version
fi
"""


def allocated_bytes(*directories: Path) -> int:
    size = 0
    for directory in directories:
        for path in directory.rglob("*"):
            info = path.lstat()
            blocks = getattr(info, "st_blocks", None)
            size += info.st_size if blocks is None else blocks * 512
    return size


class DockerNodeRuntime:
    def __init__(self, settings: Settings, backend: Literal["dsh", "pi"]) -> None:
        self.backend = backend
        self.package = PACKAGES[backend]
        self.version = getattr(settings, backend + "_version")
        self.docker = settings.docker_bin
        self.platform = "linux/amd64" if settings.container_arch == "x64" else "linux/arm64"
        self.data_dir = settings.data_dir.resolve()
        self.root = self.data_dir / "tools" / "docker-node"
        self.max_mb = (
            settings.node_runtime_max_mb if backend == "dsh" else settings.pi_node_runtime_max_mb
        )
        tag = f"{NODE_VERSION}-linux-{settings.container_arch}"
        self.node = self.root / f"node-{tag}"
        self.agent = self.root / f"{backend}-{self.version}-node-{tag}"
        self.current = {self.node, *(self.root / f"{name}-{getattr(settings, name + '_version')}-node-{tag}"
                                     for name in PACKAGES)}
        self.target = f"/opt/sca-{backend}"
        self.cli = f"{self.target}/node_modules/.bin/{backend}"

    async def mounts(self, *, node_only: bool = False) -> list[Mount]:
        await asyncio.to_thread(self.prepare)
        mounts = [Mount(source=str(self.node), target=NODE_RUNTIME_DIR, read_only=True)]
        if not node_only:
            mounts.append(Mount(source=str(self.agent), target=self.target, read_only=True))
        return mounts

    def installed(self, directory: Path) -> bool:
        modules = directory / "node_modules"
        cli = (modules / ".bin" / self.backend).resolve()
        manifest = (modules / self.package / "package.json").resolve()
        if not (cli.is_relative_to(modules) and manifest.is_relative_to(modules)):
            return False
        try:
            version = json.loads(manifest.read_bytes())["version"]
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return version == self.version and cli.is_file()

    def prepare(self) -> None:
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?(?:\+[a-zA-Z0-9.-]+)?", self.version):
            raise SandboxError(f"SCA_{self.backend.upper()}_VERSION 必须是明确的 npm 版本号")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.root / ".install.lock").open("a+b") as lock:
            lock_file(lock)
            need_node = not os.access(self.node / "bin" / "node", os.X_OK)
            if need_node or not self.installed(self.agent):
                name = "DeepSeek Harness" if self.backend == "dsh" else "Pi"
                print(f"准备 Docker 模式的 {name} {self.version} 运行时…", flush=True)
                size = self._install(need_node)
                print(f"{name} Docker 运行时已准备（{size / BYTES_PER_MB:.0f} MB）", flush=True)
            self._prune_versions()

    def _install(self, need_node: bool) -> int:
        required_mb = self.max_mb + INSTALL_HEADROOM_MB
        free_mb = shutil.disk_usage(self.data_dir).free // BYTES_PER_MB
        if free_mb < required_mb:
            raise SandboxError(
                f"{self.backend} Docker 运行时安装磁盘空间不足"
                f"（SCA_DATA_DIR 需要至少 {required_mb} MB，可用 {free_mb} MB）"
            )
        with tempfile.TemporaryDirectory(prefix=".install-", dir=self.root) as directory:
            stage = Path(directory)
            name = f"sca-node-install-{uuid4().hex}"
            try:
                subprocess.run([
                    self.docker, "run", "--rm", "--name", name, "--platform", self.platform,
                    "--label", f"{NODE_RUNTIME_LABEL}=installer",
                    "--user", f"{os.getuid()}:{os.getgid()}",
                    "--mount", f"type=bind,source={stage},target=/runtime",
                    "--workdir", "/runtime", "--env", "HOME=/tmp",
                    "--env", "npm_config_cache=/tmp/npm-cache",
                    "--env", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1",
                    "--env", "PUPPETEER_SKIP_DOWNLOAD=1",
                    NODE_IMAGE, "sh", "-ec", INSTALL, "sca-node-runtime",
                    "copy" if need_node else "reuse", f"{self.package}@{self.version}", self.backend,
                ], check=True, timeout=240, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                with suppress(OSError, subprocess.SubprocessError):
                    subprocess.run([self.docker, "rm", "-f", name], check=False, timeout=15,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                raise SandboxError(f"准备 {self.backend} Docker 运行时超时，请检查 Docker 和 npm 网络") from None
            except (OSError, subprocess.CalledProcessError):
                raise SandboxError(
                    f"无法准备 {self.backend} Docker 运行时，请检查 Docker、npm 网络和磁盘空间"
                ) from None
            node = stage / "node" if need_node else self.node
            agent = stage / "agent"
            if not os.access(node / "bin" / "node", os.X_OK) or not self.installed(agent):
                raise SandboxError(f"{self.backend} Docker 运行时安装不完整")
            size = allocated_bytes(node, agent)
            if size > self.max_mb * BYTES_PER_MB:
                raise SandboxError(
                    f"{self.backend} Docker 运行时超过磁盘上限"
                    f"（占用 {ceil(size / BYTES_PER_MB)} MB，上限 {self.max_mb} MB）"
                )
            if need_node:
                self._publish(node, self.node)
            self._publish(agent, self.agent)
            return size

    @staticmethod
    def _publish(stage: Path, destination: Path) -> None:
        if destination.is_dir():
            shutil.rmtree(destination)
        stage.rename(destination)

    def _prune_versions(self) -> None:
        old = {path for path in self.root.iterdir()
               if path.is_dir() and not path.is_symlink() and path not in self.current
               and path.name.startswith(("node-", "dsh-", "pi-", ".install-"))}
        if not old:
            return
        # Containers that still bind an old version must be able to finish their work.
        try:
            ids = subprocess.check_output(
                [self.docker, "ps", "-aq", "--filter", f"label={NODE_RUNTIME_LABEL}"],
                text=True, timeout=15, stderr=subprocess.DEVNULL,
            ).split()
            mounted = subprocess.check_output([
                self.docker, "inspect", "--format", "{{range .Mounts}}{{println .Source}}{{end}}", *ids,
            ], text=True, timeout=15, stderr=subprocess.DEVNULL).splitlines() if ids else []
        except (OSError, subprocess.SubprocessError):
            return
        for path in old - {Path(source).resolve() for source in mounted if source}:
            shutil.rmtree(path)


if __name__ == "__main__":
    from sca.config import get_settings

    try:
        DockerNodeRuntime(get_settings(), "dsh").prepare()
    except SandboxError as exc:
        raise SystemExit(str(exc)) from None
    except (OSError, ValueError):
        raise SystemExit("无法准备 DeepSeek Harness Docker 运行时，请检查运行时目录和配置") from None
