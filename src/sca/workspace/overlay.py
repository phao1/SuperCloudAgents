"""Docker-owned overlay mounts; repository credentials stay in the host mirror."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath
from uuid import uuid4

from sca.core.errors import WorkspaceError
from sca.core.models import RepoCredential
from sca.workspace.cache import RepoCache
from sca.workspace.git import DEFAULT_EXCLUDES, GitWorkspace, Prepared
from sca.workspace.layers import LayerStore, command

VOLUME = re.compile(r"sca-ws-[a-zA-Z0-9_-]+\Z")
CLEANUP_SCRIPT = """
import pathlib
import shutil
for name in ('upper', 'work'):
    path = pathlib.Path('/cleanup') / name
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
"""
WHITEOUT_SCRIPT = """
import json
import pathlib
import shutil
import sys
for name in json.loads(sys.argv[1]):
    path = pathlib.Path(name)
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError('symlink parent')
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()
"""


def volume_name(task_dir: Path) -> str:
    name = f"sca-ws-{task_dir.name}"
    if not VOLUME.fullmatch(name):
        raise WorkspaceError("Invalid overlay task identifier")
    return name


def helper_argv(volume: str, image: str, argv: list[str]) -> list[str]:
    if not VOLUME.fullmatch(volume):
        raise WorkspaceError("Invalid overlay volume")
    return ["docker", "run", "--rm", "--network=none", "--read-only", "--tmpfs", "/tmp",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory=256m", "--cpus=1",
            "--user", f"{os.getuid()}:{os.getgid()}", "--volume", f"{volume}:/w",
            "--workdir", "/w", "--entrypoint", argv[0], image, *argv[1:]]


class OverlayWorkspace(GitWorkspace):
    def __init__(self, task_dir: Path, branch: str, cache: RepoCache | None = None,
                 layers: LayerStore | None = None, *, image: str = "sca-sandbox:latest"):
        super().__init__(task_dir, branch, cache=cache, layers=layers)
        self.image = image
        self.overlay = self.task_dir / "overlay"
        self.upper = self.overlay / "upper"
        self.volume = volume_name(self.task_dir)
        self.layers = self.layers or LayerStore(self.task_dir.parent.parent / "layers")

    async def helper(self, argv: list[str]) -> str:
        name = f"sca-task-{self.task_dir.name}-helper-{uuid4().hex}"
        args = helper_argv(self.volume, self.image, argv)
        args[2:2] = ["--name", name, "--label", f"sca.task_id={self.task_dir.name}"]
        try:
            return await command(*args)
        finally:
            with suppress(WorkspaceError):
                await command("docker", "rm", "-f", name)

    async def prepare(self, repo_url: str, base_ref: str, *, mirror_only: bool = False,
                      credential: RepoCredential | None = None,
                      tenant_id: str = "default") -> Prepared:
        if (self.task_dir / "workspace.overlay").exists():
            await self.cleanup()
        return await super().prepare(repo_url, base_ref, mirror_only=mirror_only,
                                     credential=credential, tenant_id=tenant_id)

    async def build_workspace(self, commit: str) -> Prepared:
        if (self.task_dir / "workspace.ready").exists():
            await self.ensure_volume()
            return Prepared(commit, self.branch, self.workspace)
        metadata = self.task_dir / "layer-source.json"
        source = json.loads(metadata.read_text()) if metadata.exists() else {
            "repo_key": str(self.mirror), "tenant_id": "default"}
        metadata.write_text(json.dumps(source))
        self.overlay.mkdir(parents=True, exist_ok=True)
        key = await self.layers.copy(self.mirror, commit, source["repo_key"], self.overlay,
                                     source["tenant_id"], overlay=True)
        for name in ("upper", "work"):
            (self.overlay / name).mkdir(exist_ok=True)
        (self.task_dir / "workspace.overlay").write_text(json.dumps({
            "volume": self.volume, "lower": str(self.layers.root / key)}))
        await self.ensure_volume()
        await self.helper(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                           "checkout", "-B", self.branch, commit])
        await self.helper(["python3", "-c",
                           "from pathlib import Path; p=Path('.git/info/exclude'); "
                           "p.parent.mkdir(exist_ok=True); "
                           "p.open('a').write('\\n'+" + repr("\n".join(DEFAULT_EXCLUDES)) + "+'\\n')"])
        (self.workspace / ".sca").mkdir(parents=True, exist_ok=True)
        (self.task_dir / "workspace.ready").write_text(commit)
        return Prepared(commit, self.branch, self.workspace)

    async def ensure_volume(self) -> None:
        metadata = json.loads((self.task_dir / "workspace.overlay").read_text())
        lower = Path(metadata["lower"])
        if lower.parent != self.layers.root or not re.fullmatch(r"[0-9a-f]{64}", lower.name):
            raise WorkspaceError("Invalid overlay lower layer")
        paths = [lower, self.upper, self.overlay / "work"]
        if any(any(c in str(p) for c in ",:\n") or p.is_symlink() for p in paths):
            raise WorkspaceError("Invalid overlay directories")
        await command("docker", "volume", "create", "--driver", "local",
                      "--label", "sca.workspace=overlay", "--label", f"sca.task_id={self.task_dir.name}",
                      "--opt", "type=overlay", "--opt", "device=overlay", "--opt",
                      f"o=lowerdir={lower},upperdir={self.upper},workdir={self.overlay / 'work'},"
                      "redirect_dir=off,index=off,metacopy=off",
                      self.volume)

    async def fetch(self) -> str:
        # The helper exports bytes over stdout; no writable host bind enters the helper.
        with tempfile.TemporaryDirectory(dir=self.task_dir) as directory:
            encoded = await self.helper(["python3", "-c",
                ("import base64,subprocess; "
                "subprocess.run(['git','-c','core.hooksPath=/dev/null',"
                "'-c','core.fsmonitor=false','bundle','create','/tmp/result.bundle','HEAD'],"
                "check=True,stdout=subprocess.DEVNULL); "
                "print(base64.b64encode(open('/tmp/result.bundle','rb').read()).decode())")])
            bundle = Path(directory) / "result.bundle"
            bundle.write_bytes(base64.b64decode(encoded, validate=True))
            return await self.fetch_bundle(bundle)

    async def remove_volume(self) -> None:
        names = await command("docker", "volume", "ls", "--format", "{{.Name}}")
        if self.volume in names.splitlines():
            await command("docker", "volume", "rm", self.volume)

    async def clear_upper(self) -> None:
        if not self.overlay.exists():
            return
        # Docker owns work/work (mode 000); only this cleanup helper runs as root.
        await command("docker", "run", "--rm", "--network=none", "--read-only",
                      "--cap-drop=ALL", "--cap-add=DAC_OVERRIDE", "--cap-add=FOWNER",
                      "--security-opt=no-new-privileges", "--memory=128m", "--cpus=1",
                      "--user=0", "--volume", f"{self.overlay}:/cleanup",
                      "--entrypoint=python3", self.image, "-c",
                      CLEANUP_SCRIPT)
        self.overlay.rmdir()

    async def cleanup(self) -> None:
        await self.remove_volume()
        await self.clear_upper()
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        for name in ("workspace.overlay", "overlay.layer", "workspace.ready"):
            (self.task_dir / name).unlink(missing_ok=True)

    def snapshot(self) -> bytes:
        whiteouts: list[str] = []
        opaque: dict[str, str] = {}

        def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            name = info.name.removeprefix("./")
            path = self.upper / name
            if info.ischr() and info.devmajor == 0 and info.devminor == 0:
                whiteouts.append(name)
                return None
            if not (info.isfile() or info.isdir() or info.issym() or info.islnk()):
                raise WorkspaceError("Unsupported overlay snapshot entry")
            if info.isdir() and "user.overlay.opaque" in os.listxattr(path):
                opaque[name] = os.getxattr(path, "user.overlay.opaque").decode("ascii")
            return info

        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            tar.add(self.upper, arcname=".", filter=keep)
        source = json.loads((self.task_dir / "layer-source.json").read_text())
        metadata = {"base": (self.task_dir / "workspace.ready").read_text(),
                    "source": source, "whiteouts": whiteouts, "opaque": opaque}
        # One envelope keeps metadata and archive under the manifest checksum.
        context = io.BytesIO()
        from sca.workspace.tarsafe import pack

        pack(self.workspace, context)
        return json.dumps({"metadata": metadata,
                           "context": base64.b64encode(context.getvalue()).decode(),
                           "archive": base64.b64encode(archive.getvalue()).decode()}).encode()

    async def restore_snapshot(self, blob: bytes) -> None:
        from sca.workspace.tarsafe import extract

        envelope = json.loads(blob)
        metadata = envelope["metadata"]
        for name in [*metadata["whiteouts"], *metadata["opaque"]]:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise WorkspaceError("Invalid overlay snapshot path")
        if (self.task_dir / "workspace.overlay").exists():
            await self.remove_volume()
        await self.clear_upper()
        (self.task_dir / "workspace.ready").unlink(missing_ok=True)
        (self.task_dir / "layer-source.json").write_text(json.dumps(metadata["source"]))
        await self.build_workspace(metadata["base"])
        # Deleting through the merged view asks the kernel to recreate whiteouts.
        await self.helper(["python3", "-c", WHITEOUT_SCRIPT,
                           json.dumps(metadata["whiteouts"])])
        # The volume must be unmounted before manipulating its upper directory.
        await self.remove_volume()
        await asyncio.to_thread(
            extract, io.BytesIO(base64.b64decode(envelope["archive"], validate=True)), self.upper)
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        await asyncio.to_thread(
            extract, io.BytesIO(base64.b64decode(envelope["context"], validate=True)), self.workspace)
        for name in metadata["opaque"]:
            path = self.upper / name
            if path.is_symlink() or any(p.is_symlink() for p in path.parents):
                raise WorkspaceError("Invalid opaque directory")
            if not stat.S_ISDIR(path.stat().st_mode):
                raise WorkspaceError("Invalid opaque directory")
            os.setxattr(path, "user.overlay.opaque", metadata["opaque"][name].encode("ascii"))
        await self.ensure_volume()
