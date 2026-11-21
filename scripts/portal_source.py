"""Install pinned native release binaries and launch the source-mode model gateway."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen
from zipfile import BadZipFile, ZipFile

TEMPORAL_VERSION = "1.5.0"
OPENCODE_VERSION = "2.3.0"
OPENCODE_COMMIT = "a6b15d33daf6ab7d35569f6d711fdb630b9f04e7"
OPENCODE_REPO = "https://github.com/anomalyco/opencode"

# Published SHA-256 digests from the GitHub release assets for the two tags.
TEMPORAL_DIGESTS = {
    ("darwin", "x64"): "5060e803598bb17b14fc8d40cfa1f50bdd6f2f3ad346e3b042f7c761dd7393ee",
    ("darwin", "arm64"): "7437c43acc82416e8a612aeabe396bc1fb78efca0b648afb0253ad30c511b8e3",
    ("linux", "x64"): "0e847562a59ac7cbed38893bfd21944da4f2ff1213339963c79edf3685cc0c55",
    ("linux", "arm64"): "6533d3399f3620ebb5356514e2e85865785e7fd16294df516000e2e7c56cd8a6",
}
OPENCODE_DIGESTS = {
    ("darwin", "x64"): "b4985f65e2e4d624ebe5a490df66cbbce06a4146b8ddfab86473f73c2cc57ea2",
    ("darwin", "arm64"): "7907df2b12720a840fbb49c5b978b9b82925b6bf75479a5fd216afa8932b6702",
    ("linux", "x64"): "fc0f81522aceeabac47693d88389efa2bc517ce74c2c38fd6967819fcc6e4d3b",
    ("linux", "arm64"): "3207fb5cdec448d479242c0f1a14f4f7bdfcad3a79209b5eb8f9003725e93711",
}


@dataclass(frozen=True)
class Release:
    url: str
    digest: str
    binary: Path


def releases(root: Path, system: str, machine: str) -> tuple[Release, Release]:
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine.lower()
    )
    system = system.lower()
    if arch is None or system not in ("darwin", "linux"):
        raise ValueError("源码模式支持 Linux 和 macOS 的 x86_64 或 arm64。")
    temporal_arch = "amd64" if arch == "x64" else arch
    temporal_name = f"temporal_cli_{TEMPORAL_VERSION}_{system}_{temporal_arch}.tar.gz"
    target = f"{system}-{arch}" + ("-baseline" if arch == "x64" else "")
    extension = "zip" if system == "darwin" else "tar.gz"
    return (
        Release(
            f"https://github.com/temporalio/cli/releases/download/v{TEMPORAL_VERSION}/{temporal_name}",
            TEMPORAL_DIGESTS[system, arch], root / "temporal",
        ),
        Release(
            f"{OPENCODE_REPO}/releases/download/v{OPENCODE_VERSION}/opencode-{target}.{extension}",
            OPENCODE_DIGESTS[system, arch], root / "opencode" / f"{OPENCODE_VERSION}-sca.0" / "opencode",
        ),
    )


def install(release: Release) -> None:
    destination = release.binary
    stamp = destination.with_suffix(".sha256")
    if (destination.is_file() and os.access(destination, os.X_OK) and stamp.is_file()
            and stamp.read_text(encoding="ascii").strip() == release.digest):
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载并校验 {destination.name}…", flush=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        temporary = Path(directory)
        archive = temporary / "download"
        with urlopen(release.url, timeout=30) as response, archive.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != release.digest:
                raise ValueError(f"{destination.name} 的 SHA-256 校验失败。")
        binary = temporary / destination.name
        with binary.open("wb") as output:
            if release.url.endswith(".zip"):
                with ZipFile(archive) as zipped, zipped.open(destination.name) as stream:
                    shutil.copyfileobj(stream, output)
            else:
                with tarfile.open(archive, "r:gz") as tar:
                    member = next((entry for entry in tar
                                   if entry.name.removeprefix("./") == destination.name), None)
                    if member is None or not member.isfile():
                        raise ValueError(f"{destination.name} 的归档格式无效。")
                    with tar.extractfile(member) as stream:
                        shutil.copyfileobj(stream, output)
        binary.chmod(0o700)
        binary.replace(destination)
        verified = temporary / "verified"
        verified.write_text(release.digest + "\n", encoding="ascii")
        verified.replace(stamp)


def install_binaries(root: Path) -> None:
    temporal, opencode = releases(root, platform.system(), platform.machine())
    for release in (temporal, opencode):
        try:
            install(release)
        except (OSError, KeyError, tarfile.TarError, BadZipFile):
            raise ValueError(f"无法下载或解包 {release.binary.name}，请检查网络和磁盘空间。") from None
    metadata = opencode.binary.with_name("meta.json")
    if not metadata.exists():
        temporary = metadata.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "version": metadata.parent.name, "source_repo": OPENCODE_REPO + ".git",
            "source_ref": f"v{OPENCODE_VERSION}", "commit": OPENCODE_COMMIT,
            "notes": "Native upstream OpenCode release",
        }), encoding="utf-8")
        temporary.replace(metadata)


def select_port(port: int, strict: bool) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise ValueError(f"无法检查本机端口 {port}。") from exc
            if strict:
                raise ValueError(f"本机端口 {port} 已被占用。若为 Docker 服务，请先运行 down --all。")
            probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def serve_gateway(config: str) -> None:
    from dotenv import load_dotenv

    database_url = os.environ["DATABASE_URL"]
    # Compose's env_file takes precedence for provider keys, then sets DATABASE_URL explicitly.
    load_dotenv(".env", override=True)
    os.environ["DATABASE_URL"] = database_url
    from litellm.proxy.proxy_cli import run_server

    run_server(args=["--config", config, "--host", "127.0.0.1",
                     "--port", "4000", "--num_workers", "1"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install").add_argument("directory", type=Path)
    port = commands.add_parser("port")
    port.add_argument("preferred", type=int)
    port.add_argument("--strict", action="store_true")
    commands.add_parser("postgres-password").add_argument("path", type=Path)
    commands.add_parser("gateway").add_argument("config")
    args = parser.parse_args()
    try:
        match args.command:
            case "install":
                install_binaries(args.directory)
            case "port":
                print(select_port(args.preferred, args.strict))
            case "postgres-password":
                with args.path.open("x", encoding="utf-8") as stream:
                    stream.write(secrets.token_hex(32) + "\n")
            case "gateway":
                serve_gateway(args.config)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
