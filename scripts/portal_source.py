"""Prepare native source-mode dependencies and manage the Windows Portal services."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen
from zipfile import BadZipFile, ZipFile

TEMPORAL_VERSION = "1.5.0"
LITELLM_VERSION = "1.110.2"
OPENCODE_VERSION = "2.3.0"
OPENCODE_COMMIT = "a6b15d33daf6ab7d35569f6d711fdb630b9f04e7"
OPENCODE_REPO = "https://github.com/anomalyco/opencode"
DSH_NATIVE_PACKAGES = ("@deepseek-ai/dsh-subprocess-local", "koffi", "node-pty")

# Published SHA-256 digests from the GitHub release assets for the two tags.
TEMPORAL_DIGESTS = {
    ("darwin", "x64"): "5060e803598bb17b14fc8d40cfa1f50bdd6f2f3ad346e3b042f7c761dd7393ee",
    ("darwin", "arm64"): "7437c43acc82416e8a612aeabe396bc1fb78efca0b648afb0253ad30c511b8e3",
    ("linux", "x64"): "0e847562a59ac7cbed38893bfd21944da4f2ff1213339963c79edf3685cc0c55",
    ("linux", "arm64"): "6533d3399f3620ebb5356514e2e85865785e7fd16294df516000e2e7c56cd8a6",
    ("windows", "x64"): "f44ca7a0eab7dba34070f306619af160d0a4e0255f002abd9b66e3d3834393da",
    ("windows", "arm64"): "775a09a90664ca65370e0037550e0d1f3944fec94f9766b85ff2f66818c13d53",
}
OPENCODE_DIGESTS = {
    ("darwin", "x64"): "b4985f65e2e4d624ebe5a490df66cbbce06a4146b8ddfab86473f73c2cc57ea2",
    ("darwin", "arm64"): "7907df2b12720a840fbb49c5b978b9b82925b6bf75479a5fd216afa8932b6702",
    ("linux", "x64"): "fc0f81522aceeabac47693d88389efa2bc517ce74c2c38fd6967819fcc6e4d3b",
    ("linux", "arm64"): "3207fb5cdec448d479242c0f1a14f4f7bdfcad3a79209b5eb8f9003725e93711",
    ("windows", "x64"): "a3abde3f736a1b51acb28466001333189184507096cb2fe0d684fe822cbd37ec",
    ("windows", "arm64"): "86217da9616447404b96c599bee08a1c2d932c92fb58b0f60cfe835e2b61926a",
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
    if arch is None or (system, arch) not in TEMPORAL_DIGESTS:
        raise ValueError("源码模式支持 Linux、macOS 和 Windows 的 x86_64 或 arm64。")
    windows = system == "windows"
    suffix = ".exe" if windows else ""
    temporal_arch = "amd64" if arch == "x64" else arch
    temporal_format = "zip" if windows else "tar.gz"
    temporal_name = f"temporal_cli_{TEMPORAL_VERSION}_{system}_{temporal_arch}.{temporal_format}"
    target = f"{system}-{arch}" + ("-baseline" if arch == "x64" else "")
    extension = "tar.gz" if system == "linux" else "zip"
    return (
        Release(
            f"https://github.com/temporalio/cli/releases/download/v{TEMPORAL_VERSION}/{temporal_name}",
            TEMPORAL_DIGESTS[system, arch], root / f"temporal{suffix}",
        ),
        Release(
            f"{OPENCODE_REPO}/releases/download/v{OPENCODE_VERSION}/opencode-{target}.{extension}",
            OPENCODE_DIGESTS[system, arch],
            root / "opencode" / f"{OPENCODE_VERSION}-sca.0" / f"opencode{suffix}",
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


def install_binaries(root: Path, *, include_opencode: bool = True) -> None:
    temporal, opencode = releases(root, platform.system(), platform.machine())
    for release in (temporal, opencode) if include_opencode else (temporal,):
        try:
            install(release)
        except (OSError, KeyError, tarfile.TarError, BadZipFile):
            raise ValueError(f"无法下载或解包 {release.binary.name}，请检查网络和磁盘空间。") from None
    if not include_opencode:
        return
    metadata = opencode.binary.with_name("meta.json")
    if not metadata.exists():
        temporary = metadata.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "version": metadata.parent.name, "source_repo": OPENCODE_REPO + ".git",
            "source_ref": f"v{OPENCODE_VERSION}", "commit": OPENCODE_COMMIT,
            "notes": "Native upstream OpenCode release",
        }), encoding="utf-8")
        temporary.replace(metadata)


def _bind_loopback(probe: socket.socket, port: int) -> None:
    address = ("127.0.0.1", port)
    try:
        probe.bind(address)
    except OSError as exc:
        if sys.platform == "win32" or exc.errno != errno.EADDRINUSE:
            raise
        # A reusable bind can shadow a live wildcard listener on macOS.
        # Check for a listener before retrying a bind blocked by TIME_WAIT.
        with socket.socket() as connection:
            connection.settimeout(0.25)
            if connection.connect_ex(address) != errno.ECONNREFUSED:
                raise
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(address)


def select_port(port: int, strict: bool) -> int:
    try:
        probe = socket.socket()
    except OSError:
        raise ValueError("无法创建本机套接字，请检查运行环境。") from None
    with probe:
        if sys.platform == "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            _bind_loopback(probe, port)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", None) not in (10013, 10048):
                raise ValueError(f"无法检查本机端口 {port}。") from exc
            if strict:
                hint = ("请先停止占用端口的服务。" if sys.platform == "win32"
                        else "若为 Docker 服务，请先运行 down --all。")
                raise ValueError(f"本机端口 {port} 已被占用。{hint}") from None
            probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def venv_bin(venv: Path, name: str) -> Path:
    return venv / "Scripts" / f"{name}.exe" if sys.platform == "win32" else venv / "bin" / name


def run_logged(argv: list[str | Path], log: Path, *, cwd: Path | None = None,
               env: dict[str, str] | None = None, input_text: str | None = None,
               timeout: int = 900) -> None:
    try:
        with log.open("ab") as output:
            subprocess.run(
                [str(arg) for arg in argv], cwd=cwd, env=env, input=input_text,
                encoding="utf-8", errors="replace", stdout=output, stderr=subprocess.STDOUT,
                check=True, timeout=timeout,
            )
    except (OSError, subprocess.SubprocessError):
        raise ValueError(f"{Path(argv[0]).stem} 执行失败，请查看 {log}。") from None


def transfer_history(command: str, archive: Path, *, migrate: bool) -> int:
    from sca.config import get_settings

    try:
        settings = get_settings()
    except (OSError, ValueError):
        raise ValueError("无法加载 Portal 配置，请检查本机配置和环境变量。") from None
    fields = {"database_url", "data_dir", "object_store", "default_max_active_tasks"}
    if settings.object_store == "s3":
        fields.update({"s3_bucket", "s3_endpoint_url", "s3_access_key", "s3_secret_key", "s3_region"})
    # The standalone history CLI reads storage configuration from the process environment.
    storage = settings.model_dump(include=fields, exclude_none=True)
    env = {**os.environ, **{f"SCA_{key.upper()}": str(value) for key, value in storage.items()}}
    if migrate:
        try:
            subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        except (OSError, subprocess.SubprocessError):
            raise ValueError("历史数据库迁移失败，请检查本机 PostgreSQL 和迁移版本。") from None
    args = ["--out", str(archive)] if command == "export" else [str(archive)]
    return subprocess.call([sys.executable, "-m", "sca.history", command, *args], env=env)


def install_dsh(data: Path) -> None:
    from sca.agents.local import DSH_PACKAGE, dsh_install_dir, installed_dsh
    from sca.config import Settings
    from sca.host import host_executable

    settings = Settings(data_dir=data, sandbox_driver="local")
    prefix = dsh_install_dir(settings)
    stamp = prefix / ".sca-native-scripts"
    if (installed_dsh(settings) and stamp.is_file()
            and stamp.read_text(encoding="utf-8").strip() == settings.dsh_version):
        return
    npm = host_executable("npm.cmd" if sys.platform == "win32" else "npm")
    if npm is None:
        raise ValueError("安装 DeepSeek Harness 需要 npm。Windows 可运行 "
                         "winget install -e --id OpenJS.NodeJS.LTS。")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?(?:\+[a-zA-Z0-9.-]+)?",
                        settings.dsh_version):
        raise ValueError("SCA_DSH_VERSION 必须是明确的 npm 版本号。")
    prefix.mkdir(parents=True, exist_ok=True)
    print("安装本机 DeepSeek Harness…", flush=True)
    command: list[str | Path] = [npm]
    if sys.platform == "win32":
        # Invoke npm's JS entry directly so a checkout path never passes through cmd.exe.
        node = host_executable("node.exe")
        entry = npm.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if node is None or not entry.is_file():
            raise ValueError("找不到 npm CLI，请安装 Node.js LTS 或检查 PATH。")
        command = [node, entry]
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("SCA_") and k not in ("PORTAL_TOKEN", "DEEPSEEK_API_KEY", "LITELLM_MASTER_KEY")}
    manifest = prefix / "package.json"
    metadata = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {"private": True}
    metadata["allowScripts"] = {**dict.fromkeys(DSH_NATIVE_PACKAGES, True),
                                "@google/genai": False, "protobufjs": False}
    manifest.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    stamp.unlink(missing_ok=True)
    log = data / "run" / "dsh.log"
    # A targeted rebuild also limits script execution on npm versions that ignore allowScripts.
    run_logged(
        [*command, "install", "--prefix", prefix, "--omit=dev", "--no-audit", "--no-fund",
         "--ignore-scripts", "--save-exact", f"{DSH_PACKAGE}@{settings.dsh_version}"], log, env=env,
    )
    run_logged([*command, "rebuild", "--prefix", prefix, "--foreground-scripts", *DSH_NATIVE_PACKAGES],
               log, env=env)
    if not installed_dsh(settings):
        raise ValueError("DeepSeek Harness 安装不完整，请查看 .data/run/dsh.log。")
    stamp.write_text(settings.dsh_version + "\n", encoding="utf-8")


def gateway_env(venv: Path, env: dict[str, str]) -> dict[str, str]:
    return {**env, "PATH": os.pathsep.join((str(venv_bin(venv, "python").parent), env.get("PATH", ""))),
            "PRISMA_BINARY_CACHE_DIR": str(venv / "prisma-cache")}


def prepare_gateway(data: Path) -> None:
    venv = data / "litellm-venv"
    python = venv_bin(venv, "python")
    stamp = venv / ".sca-version"
    if python.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == LITELLM_VERSION:
        return
    if not shutil.which("node") or not shutil.which("npm"):
        raise ValueError("LiteLLM 需要 Node.js 和 npm。Windows: winget install -e --id OpenJS.NodeJS.LTS。"
                         "Linux: sudo apt install nodejs npm。macOS: brew install node。")
    print("安装本机 LiteLLM 网关及其 PostgreSQL 客户端…", flush=True)
    log = data / "run" / "litellm.log"
    if not python.is_file():
        run_logged([sys.executable, "-m", "venv", venv], log)
    run_logged([python, "-m", "pip", "install", "-q", f"litellm[proxy]=={LITELLM_VERSION}",
                "prisma==0.11.0", "psycopg[binary]>=3.2,<4"], log)
    # Generate from the installed gateway's schema and keep the Prisma cache with its venv.
    command = ("from importlib.resources import files; from prisma.cli import main; "
               "import sys; sys.argv[1:] = ['generate', '--schema', str(files('litellm_proxy_extras')"
               ".joinpath('schema.prisma'))]; main()")
    run_logged([python, "-c", command], log, cwd=venv, env=gateway_env(venv, dict(os.environ)))
    stamp.write_text(LITELLM_VERSION + "\n", encoding="utf-8")


def write_password(path: Path) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(secrets.token_hex(32) + "\n")


def serve_gateway(config: str) -> None:
    from dotenv import load_dotenv

    database_url = os.environ["DATABASE_URL"]
    # Compose's env_file takes precedence for provider keys, then sets DATABASE_URL explicitly.
    load_dotenv(".env", override=True, encoding="utf-8")
    os.environ["DATABASE_URL"] = database_url
    # The gateway has its own venv. Load provider credentials without importing the SCA app.
    config_path = Path(config)
    if config_path.name == "litellm.generated.yaml":
        try:
            registry = json.loads(config_path.with_name("model-providers.json").read_bytes())
            keys = registry["keys"]
            if not isinstance(keys, dict) or any(not re.fullmatch(r"SCA_UPSTREAM_[0-9A-F]{32}", name)
                                                 for name in keys):
                raise ValueError("invalid provider environment")
            os.environ.update(keys)
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError("无法读取本机模型接入密钥") from None
    from litellm.proxy.proxy_cli import run_server

    run_server(args=["--config", config, "--host", "127.0.0.1",
                     "--port", "4000", "--num_workers", "1"])


# Windows source services
POSTGRES_RELEASE = "17.12-1"
POSTGRES_URL = (
    f"https://get.enterprisedb.com/postgresql/postgresql-{POSTGRES_RELEASE}-windows-x64-binaries.zip"
)
POSTGRES_HINT = (
    "可运行 winget install -e --id PostgreSQL.PostgreSQL.17，"
    "并将 SCA_POSTGRES_BIN 指向安装目录的 bin。"
)
SERVICES = ("postgres", "temporal", "litellm", "worker", "api")
API_URL = "http://127.0.0.1:8000"


def install_postgres(tools: Path) -> Path:
    print("下载并校验 PostgreSQL Windows 发行包…", flush=True)
    tools.mkdir(parents=True, exist_ok=True)
    destination = tools / f"postgresql-{POSTGRES_RELEASE}"
    try:
        with tempfile.TemporaryDirectory(dir=tools, prefix=".postgres-") as stage:
            directory = Path(stage)
            archive = directory / "postgres.zip"
            with urlopen(POSTGRES_URL + ".sha256", timeout=30) as response:
                digest = re.search(rb"\b[0-9a-fA-F]{64}\b", response.read(4096))
            if digest is None:
                raise ValueError("缺少 PostgreSQL SHA-256 校验值。")
            with urlopen(POSTGRES_URL, timeout=30) as response, archive.open("wb") as stream:
                shutil.copyfileobj(response, stream)
            with archive.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest[0].decode("ascii").lower():
                    raise ValueError("PostgreSQL SHA-256 校验失败。")
            with ZipFile(archive) as zipped:
                members = [name for name in zipped.namelist()
                           if name.startswith(("pgsql/bin/", "pgsql/lib/", "pgsql/share/"))]
                zipped.extractall(directory, members)
            staged = directory / "pgsql"
            if not pg_tools_present(staged / "bin"):
                raise ValueError("PostgreSQL 发行包缺少可执行文件。")
            staged.rename(destination)
    except (OSError, BadZipFile, ValueError):
        raise ValueError(f"无法下载、校验或解包 PostgreSQL。{POSTGRES_HINT}") from None
    return destination / "bin"


def pg_tools_present(bin_dir: Path) -> bool:
    return all((bin_dir / f"{name}.exe").is_file() for name in ("initdb", "pg_ctl", "postgres", "psql"))


def windows_user_sid() -> str:
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        return win32security.ConvertSidToStringSid(sid)
    finally:
        token.Close()


def grant_postgres_access(directory: Path, password_file: Path, server_log: Path, log: Path) -> None:
    # The restricted PostgreSQL token retains the user SID and disables the Administrators SID.
    sid = windows_user_sid()
    icacls = Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe"
    for path, flags in ((directory, "(OI)(CI)"), (password_file, ""), (server_log, "")):
        run_logged([icacls, path, "/grant", f"*{sid}:{flags}F", "/Q"], log, timeout=30)


def process_identity(pid: int) -> str | None:
    import win32api
    import win32con
    import win32event
    import win32process
    import winerror
    from pywintypes import error

    if pid <= 1:
        return None
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION | win32con.SYNCHRONIZE, False, pid,
        )
    except error as exc:
        if exc.winerror == winerror.ERROR_INVALID_PARAMETER:
            return None
        raise ValueError("无法检查进程身份，请检查 .data/run/。") from None
    try:
        if win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_TIMEOUT:
            return win32process.GetProcessTimes(handle)["CreationTime"].isoformat()
        return None
    finally:
        handle.Close()


def http_ready(url: str) -> bool:
    try:
        with urlopen(url, timeout=2):
            return True
    except OSError:
        return False


class WindowsSource:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / ".data"
        self.runtime = self.data / "run"
        self.python = venv_bin(root / ".venv", "python")
        self.pg = self.data / "pg"
        self.pg_password = self.data / "pg-password"
        self.pg_port = self.runtime / "postgres.port"
        self.pg_bin = self.runtime / "postgres.bin"
        self.temporal = self.data / "bin" / "temporal.exe"
        self.dotenv = root / ".env"
        self.portal = self.data / "portal.env"

    def set_configuration(self) -> dict[str, str]:
        from dotenv import dotenv_values, set_key

        from sca.config import portal_require_token_env

        if not self.dotenv.exists():
            self.dotenv.write_bytes((self.root / ".env.example").read_bytes())
        self.dotenv.chmod(0o600)
        values = dotenv_values(self.dotenv, interpolate=False)
        master = values.get("LITELLM_MASTER_KEY")
        if not master or master == "GENERATED_BY_SETUP":
            master = "sk-" + secrets.token_hex(32)
            set_key(self.dotenv, "LITELLM_MASTER_KEY", master)
        gateway_master = values.get("SCA_MODEL_GATEWAY_MASTER_KEY")
        if gateway_master in (None, "", "GENERATED_BY_SETUP"):
            set_key(self.dotenv, "SCA_MODEL_GATEWAY_MASTER_KEY", master)
        elif gateway_master != master:
            raise ValueError(".env 中的两个网关 master key 必须相同。")

        previous = dotenv_values(self.portal, interpolate=False)
        token = os.environ.get("PORTAL_TOKEN") or previous.get("SCA_EVAL_API_TOKEN") or secrets.token_hex(32)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise ValueError("PORTAL_TOKEN 仅允许字母、数字、下划线和连字符。")
        if previous.get("SCA_EVAL_API_TOKEN") not in (None, token) and any(
            self.owned(service) for service in ("api", "worker")
        ):
            raise ValueError("请先运行 down，再修改 PORTAL_TOKEN。")
        backend = os.environ.get("SCA_AGENT_BACKEND") or values.get("SCA_AGENT_BACKEND") or "dsh"
        if backend not in ("dsh", "fake", "opencode", "codex", "pi"):
            raise ValueError("Windows 源码模式支持 dsh、fake、opencode、codex 和 pi 后端。")
        if previous.get("SCA_AGENT_BACKEND") not in (None, backend) and self.owned("worker"):
            raise ValueError("请先运行 down，再修改默认 Agent 后端。")
        config = {
            "SCA_RUNTIME_MODE": "source",
            "SCA_AGENT_BACKEND": backend,
            "SCA_DEFAULT_MODEL": os.environ.get("SCA_DEFAULT_MODEL") or values.get("SCA_DEFAULT_MODEL")
                                 or "deepseek-flash",
            "SCA_API_TOKENS": f"{token}:eval:admin",
            "SCA_EVAL_API_TOKEN": token,
            "SCA_PORTAL_REQUIRE_TOKEN": portal_require_token_env(
                previous.get("SCA_PORTAL_REQUIRE_TOKEN"), values.get("SCA_PORTAL_REQUIRE_TOKEN"),
            ),
            "SCA_PLATFORM_ADMIN_TOKEN": previous.get("SCA_PLATFORM_ADMIN_TOKEN") or secrets.token_hex(32),
        }
        # Use dotenv syntax compatible with both the API and gateway restart launcher.
        with tempfile.NamedTemporaryFile(dir=self.data, mode="w", encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            for key, value in config.items():
                set_key(temporary, key, value, export=True, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self.portal)
        finally:
            temporary.unlink(missing_ok=True)
        return config

    def environment(self, config: dict[str, str]) -> dict[str, str]:
        port = int(self.pg_port.read_text(encoding="utf-8"))
        password = self.pg_password.read_text(encoding="utf-8").strip()
        address = f"sca:{password}@127.0.0.1:{port}"
        return {**os.environ, **config, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
                "SCA_SANDBOX_DRIVER": "local", "SCA_STATE_TRANSPORT": "sync",
                "SCA_PORTAL_LOCAL_CONFIG": str(self.dotenv),
                "SCA_DATA_DIR": str(self.data), "SCA_API_URL": API_URL,
                "SCA_DATABASE_URL": f"postgresql+asyncpg://{address}/sca",
                "DATABASE_URL": f"postgresql://{address}/litellm",
                "SCA_TEMPORAL_ADDRESS": "127.0.0.1:7233",
                "SCA_MODEL_GATEWAY_URL": "http://127.0.0.1:4000"}

    def find_postgres(self, *, download: bool = False) -> Path:
        if override := os.environ.get("SCA_POSTGRES_BIN"):
            bin_dir = Path(override).expanduser().resolve()
            if not pg_tools_present(bin_dir):
                raise ValueError(f"SCA_POSTGRES_BIN 缺少 PostgreSQL 工具。{POSTGRES_HINT}")
            return bin_dir
        candidates = []
        if self.pg_bin.is_file():
            candidates.append(Path(self.pg_bin.read_text(encoding="utf-8").strip()))
        candidates.append(self.data / "tools" / f"postgresql-{POSTGRES_RELEASE}" / "bin")
        if value := os.environ.get("PGBIN"):
            candidates.append(Path(value))
        if executable := shutil.which("initdb.exe"):
            candidates.append(Path(executable).parent)
        if program_files := os.environ.get("ProgramFiles"):
            candidates.extend(sorted((Path(program_files) / "PostgreSQL").glob("*/bin"), reverse=True))
        for candidate in candidates:
            if candidate.is_absolute() and pg_tools_present(candidate):
                return candidate
        if download:
            return install_postgres(self.data / "tools")
        raise ValueError(f"缺少 PostgreSQL 工具。{POSTGRES_HINT}")

    def start_postgres(self) -> None:
        bin_dir = self.find_postgres(download=True)
        log = self.runtime / "postgres.log"
        initialized = (self.pg / "PG_VERSION").is_file()
        if not initialized and not self.pg_password.is_file():
            write_password(self.pg_password)
        if not self.pg_password.is_file():
            raise ValueError("本机 PostgreSQL 密码文件缺失，请恢复 .data/pg-password。")
        pid_file = self.pg / "postmaster.pid"
        ctl = [str(bin_dir / "pg_ctl.exe"), "-D", str(self.pg)]
        running = pid_file.is_file() and subprocess.run(
            [*ctl, "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=10,
        ).returncode == 0
        if not running:
            self.pg.mkdir(exist_ok=True)
            # pg_ctl's restricted cmd.exe opens -l itself. Keep that file free of launcher handles.
            server_log = self.runtime / "postgres-server.log"
            server_log.touch(exist_ok=True)
            grant_postgres_access(self.pg, self.pg_password, server_log, log)
            if not initialized:
                run_logged([bin_dir / "initdb.exe", "-D", self.pg, "-U", "sca",
                            "--auth=scram-sha-256", "--encoding=UTF8", "--no-locale",
                            "--pwfile", self.pg_password], log, timeout=120)
            keep_port = self.pg_port.is_file() and any(self.owned(s) for s in ("api", "worker", "litellm"))
            port = select_port(int(self.pg_port.read_text(encoding="utf-8")) if keep_port else 5432,
                               strict=keep_port)
            run_logged([*ctl, "-l", server_log, "-w", "-t", "60", "-o", f"-h 127.0.0.1 -p {port}",
                        "start"], log, cwd=self.pg, timeout=80)
        self.pg_bin.write_text(str(bin_dir), encoding="utf-8")
        pid, _, _, port, *_ = pid_file.read_text(encoding="utf-8", errors="replace").splitlines()
        self.remember("postgres", int(pid))
        self.pg_port.write_text(port + "\n", encoding="utf-8")
        password = self.pg_password.read_text(encoding="utf-8").strip()
        sql = "".join(
            f"SELECT 'CREATE DATABASE {name}' WHERE NOT EXISTS "
            f"(SELECT FROM pg_database WHERE datname = '{name}')\\gexec\n"
            for name in ("sca", "litellm")
        )
        run_logged([bin_dir / "psql.exe", "-X", "-h", "127.0.0.1", "-p", port, "-U", "sca",
                    "-d", "postgres", "-v", "ON_ERROR_STOP=1"], log, input_text=sql,
                   env={**os.environ, "PGPASSWORD": password, "PGCLIENTENCODING": "UTF8"}, timeout=60)

    def remember(self, name: str, pid: int) -> None:
        identity = process_identity(pid)
        if identity is None:
            raise ValueError(f"{name} 启动失败，请查看 .data/run/{name}.log。")
        (self.runtime / f"{name}.identity").write_text(identity, encoding="utf-8")
        (self.runtime / f"{name}.pid").write_text(f"{pid}\n", encoding="utf-8")

    def owned(self, name: str) -> int | None:
        try:
            pid = int((self.runtime / f"{name}.pid").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        identity = process_identity(pid)
        if identity is None:
            return None
        expected = self.runtime / f"{name}.identity"
        if not expected.is_file() or identity != expected.read_text(encoding="utf-8"):
            raise ValueError(f"{name} PID 文件与进程不匹配，请检查 .data/run/。")
        return pid

    def start(self, name: str, argv: list[str | Path], env: dict[str, str], port: int | None = None) -> None:
        if self.owned(name):
            return
        if port is not None:
            select_port(port, strict=True)
        with (self.runtime / f"{name}.log").open("ab") as log:
            process = subprocess.Popen(
                [str(arg) for arg in argv], cwd=self.root, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        self.remember(name, process.pid)

    def stop(self, name: str) -> None:
        if pid := self.owned(name):
            if name == "postgres":
                run_logged([self.find_postgres() / "pg_ctl.exe", "-D", self.pg, "-w", "-t", "60",
                            "-m", "fast", "stop"], self.runtime / "postgres.log", timeout=80)
            else:
                taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
                try:
                    subprocess.run([taskkill, "/T", "/F", "/PID", str(pid)], timeout=30,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                except (OSError, subprocess.SubprocessError):
                    raise ValueError(f"无法停止 {name}，请检查 .data/run/{name}.log。") from None
            for _ in range(30):
                if not self.owned(name):
                    break
                time.sleep(1)
            else:
                raise ValueError(f"{name} 尚未退出，请检查 .data/run/{name}.log。")
        for suffix in ("pid", "identity"):
            (self.runtime / f"{name}.{suffix}").unlink(missing_ok=True)

    def wait(self, name: str, ready: Callable[[], bool], seconds: int) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.owned(name):
                raise ValueError(f"{name} 启动失败，请查看 .data/run/{name}.log。")
            if ready():
                return
            time.sleep(1)
        raise ValueError(f"{name} 启动超时，请查看 .data/run/{name}.log。")

    def start_gateway(self, env: dict[str, str], seconds: int = 300) -> None:
        from sca.gateway.configuration import deepseek_configured
        from sca.gateway.model_providers import gateway_config, read_registry

        venv = self.data / "litellm-venv"
        config = gateway_config(self.root, self.data, read_registry(self.data),
                                deepseek=deepseek_configured(self.dotenv), docker=False)
        self.start("litellm", [venv_bin(venv, "python"), Path(__file__).resolve(), "gateway",
                               config], gateway_env(venv, env), 4000)
        self.wait("litellm", lambda: http_ready("http://127.0.0.1:4000/health/readiness"), seconds)

    def up(self) -> None:
        from sca.agents.local import host_agent_binary, unavailable_source_backend
        from sca.config import Settings
        from sca.host import host_executable, windows_bash

        if not host_executable("git.exe"):
            raise ValueError("缺少 Git，请运行 winget install -e --id Git.Git。")
        try:
            windows_bash()
        except FileNotFoundError as exc:
            raise ValueError(str(exc)) from None
        node = host_executable("node.exe")
        if node is None or not host_executable("npm.cmd"):
            raise ValueError("缺少 Node.js 或 npm，请运行 winget install -e --id OpenJS.NodeJS.LTS。")
        node_version = subprocess.check_output([node, "--version"], encoding="utf-8", errors="replace",
                                               timeout=10).strip().removeprefix("v")
        if tuple(int(part) for part in node_version.split(".")[:2]) < (24, 9):
            raise ValueError("DeepSeek Harness 需要 Node.js 24.9 或更高的 LTS 版本。")
        config = self.set_configuration()
        backend = config["SCA_AGENT_BACKEND"]
        install_binaries(self.data / "bin", include_opencode=backend == "opencode")
        install_dsh(self.data)
        settings = Settings(data_dir=self.data, sandbox_driver="local", agent_backend=backend)
        if backend != "fake" and host_agent_binary(settings, backend) is None:
            raise ValueError(unavailable_source_backend(backend))
        prepare_gateway(self.data)
        self.start_postgres()
        env = self.environment(config)
        # Every up must apply migrations against the running source-mode database.
        run_logged([self.python, "-m", "alembic", "upgrade", "head"],
                   self.runtime / "migrations.log", cwd=self.root, env=env, timeout=120)
        temporal_dir = self.data / "temporal"
        temporal_dir.mkdir(exist_ok=True)
        self.start("temporal", [self.temporal, "server", "start-dev", "--db-filename",
                               temporal_dir / "dev.db", "--ip", "127.0.0.1", "--headless"], env, 7233)

        def temporal_ready() -> bool:
            return subprocess.run(
                [self.temporal, "operator", "cluster", "health", "--address", env["SCA_TEMPORAL_ADDRESS"],
                 "--command-timeout", "1s"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=5,
            ).returncode == 0

        self.wait("temporal", temporal_ready, 60)
        self.start_gateway(env)
        self.start("worker", [self.python, "-m", "sca.orchestration.worker"], env)
        self.start("api", [self.python, "-m", "sca.api.portal_server",
                           "--no-proxy-headers", "--host", "127.0.0.1", "--port", "8000"], env, 8000)
        self.wait("api", lambda: http_ready(API_URL + "/healthz"), 120)
        if not self.owned("worker"):
            raise ValueError("worker 启动失败，请查看 .data/run/worker.log。")
        print(f"Portal {API_URL}/portal")
        print("如需令牌，请在本机查看 .data/portal.env 的 SCA_EVAL_API_TOKEN。")

    def history(self, command: str, archive: Path) -> int:
        self.start_postgres()
        return subprocess.call(
            [self.python, Path(__file__).resolve(), "history", command, str(archive), "--migrate"],
            cwd=self.root, env=self.environment({"SCA_RUNTIME_MODE": "source"}),
        )

    def restart_gateway(self) -> None:
        from dotenv import dotenv_values

        if not self.owned("postgres") or not self.portal.is_file():
            raise ValueError("请先运行 up 启动源码模式。")
        config = {key: value for key, value in dotenv_values(self.portal, interpolate=False).items()
                  if value is not None}
        self.stop("litellm")
        self.start_gateway(self.environment(config), 80)

    def logs(self, service: str | None) -> None:
        names = (service,) if service else SERVICES
        for name in names:
            logs = (name, "postgres-server") if name == "postgres" else (name,)
            for log in logs:
                path = self.runtime / f"{log}.log"
                if path.is_file():
                    with path.open(encoding="utf-8", errors="replace") as stream:
                        print(f"== {log} ==")
                        print("".join(deque(stream, maxlen=100)), end="")


def manage_windows(command: str, service: str | None = None, *, archive: Path | None = None) -> None:
    if sys.platform != "win32":
        raise ValueError("请使用 bash scripts/portal_source.sh 管理 Linux 或 macOS 源码模式。")
    if archive is not None:
        archive = archive.absolute()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    stack = WindowsSource(root)
    if command == "logs":
        stack.logs(service)
        return
    if sys.version_info[:2] != (3, 12):
        raise ValueError("需要 Python 3.12，请运行 winget install -e --id Python.Python.3.12。")
    if command in ("up", "import-history", "export-history"):
        stack.runtime.mkdir(parents=True, exist_ok=True)
        install_log = stack.runtime / "setup.log"
        if not stack.python.is_file():
            run_logged([sys.executable, "-m", "venv", root / ".venv"], install_log)
        project = root / ".venv" / ".sca-source-project"
        fingerprint = hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest()
        if not project.is_file() or project.read_text(encoding="utf-8").strip() != fingerprint:
            run_logged([stack.python, "-m", "pip", "install", "-q", "-e", "."], install_log)
            project.write_text(fingerprint + "\n", encoding="utf-8")
    if not stack.python.is_file():
        if command == "restart-gateway":
            raise ValueError("请先运行 up 启动源码模式。")
        print("源码模式服务未运行。")
        return
    if Path(sys.prefix).resolve() != stack.python.parent.parent:
        args = [str(archive)] if archive is not None else []
        raise SystemExit(subprocess.call([stack.python, "-X", "utf8", Path(__file__).resolve(), command, *args]))
    match command:
        case "up":
            stack.up()
        case "import-history" | "export-history":
            assert archive is not None
            raise SystemExit(stack.history(command.removesuffix("-history"), archive))
        case "down":
            failures = []
            for name in reversed(SERVICES):
                try:
                    stack.stop(name)
                except ValueError as exc:
                    failures.append(str(exc))
            if failures:
                raise ValueError("\n".join(failures))
        case "status":
            print("运行模式 source")
            for name in SERVICES:
                print(f"{name}  {'运行中' if stack.owned(name) else '未运行'}")
        case "restart-gateway":
            stack.restart_gateway()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install").add_argument("directory", type=Path)
    port = commands.add_parser("port")
    port.add_argument("preferred", type=int)
    port.add_argument("--strict", action="store_true")
    commands.add_parser("postgres-password").add_argument("path", type=Path)
    commands.add_parser("gateway").add_argument("config")
    for command in ("install-dsh", "prepare-gateway"):
        commands.add_parser(command).add_argument("data", type=Path)
    for command in ("up", "down", "status", "restart-gateway"):
        commands.add_parser(command)
    for command in ("import-history", "export-history"):
        commands.add_parser(command).add_argument("archive", type=Path)
    history = commands.add_parser("history")
    history.add_argument("action", choices=("import", "export"))
    history.add_argument("archive", type=Path)
    history.add_argument("--migrate", action="store_true")
    commands.add_parser("logs").add_argument(
        "service", nargs="?", choices=(*SERVICES, "setup", "migrations", "dsh"),
    )
    args = parser.parse_args()
    try:
        match args.command:
            case "install":
                install_binaries(args.directory)
            case "port":
                print(select_port(args.preferred, args.strict))
            case "postgres-password":
                write_password(args.path)
            case "gateway":
                serve_gateway(args.config)
            case "install-dsh":
                install_dsh(args.data)
            case "prepare-gateway":
                prepare_gateway(args.data)
            case "history":
                raise SystemExit(transfer_history(args.action, args.archive, migrate=args.migrate))
            case _:
                manage_windows(args.command, getattr(args, "service", None),
                               archive=getattr(args, "archive", None))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
