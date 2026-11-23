import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
import yaml
from fastapi import Request

from sca.agents.local import DSH_PACKAGE, dsh_install_dir, installed_dsh
from sca.api import portal
from sca.api.app import _portal_page
from sca.config import SOURCE_MODE_WARNING, SOURCE_OPENCODE_VERSION, Settings
from sca.core.models import AgentContext, AgentOverlay, ExecExit, ExecOutput, Principal
from sca.gateway.model_providers import ProviderInput, ProviderRegistry
from sca.orchestration.worker import make_agent_backend, make_gateway, make_sandbox_provider
from sca.sandbox.local import LocalSandboxProvider
from scripts import portal_source, source_smoke


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses exclusive address binding")
def test_source_port_probe_allows_restart_after_connection_closes():
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                # Close from the server side first to leave TIME_WAIT on the listener's port.
                connection.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
                client.shutdown(socket.SHUT_WR)
                assert connection.recv(1) == b""

    assert portal_source.select_port(port, strict=True) == port


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses exclusive address binding")
@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0"])
def test_source_port_probe_rejects_active_listener(address):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((address, 0))
        listener.listen()

        with pytest.raises(ValueError, match="已被占用"):
            portal_source.select_port(listener.getsockname()[1], strict=True)


def test_native_gateway_uses_generated_routes_and_loads_keys_only_in_gateway(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    shutil.copytree(root / 'deploy/litellm', tmp_path / 'deploy/litellm')
    stack = portal_source.WindowsSource(tmp_path)
    starts = []
    monkeypatch.setattr(stack, 'start', lambda *args: starts.append(args))
    monkeypatch.setattr(stack, 'wait', lambda *args: None)
    registry = ProviderRegistry().with_provider(uuid4(), ProviderInput(
        name='Native local', type='openai-compatible', base_url='http://localhost:11434/v1',
        api_key='fixture-cloud-key', models=[{'name': 'local', 'upstream_model': 'ml'}],
    ))
    registry.save(stack.data)
    stack.start_gateway({'PATH': ''})
    service, args, env, port = starts[0]
    key_env = registry.providers[0].key_env
    assert service == 'litellm' and port == 4000 and key_env not in env
    config = args[-1]
    params = yaml.safe_load(config.read_text(encoding='utf-8'))['model_list'][0]['litellm_params']
    assert params['api_base'] == 'http://localhost:11434/v1'
    assert params['api_key'] == f'os.environ/{key_env}'

    calls = []
    monkeypatch.setitem(sys.modules, 'litellm.proxy.proxy_cli', SimpleNamespace(
        run_server=lambda args: calls.append((args, os.environ[key_env])),
    ))
    monkeypatch.setenv('DATABASE_URL', 'postgresql://ignored/fixture')
    monkeypatch.setenv(key_env, '')
    monkeypatch.chdir(tmp_path)
    portal_source.serve_gateway(str(config))
    assert calls[0][0][:2] == ['--config', str(config)]
    assert calls[0][1] == 'fixture-cloud-key'


@pytest.mark.parametrize(("system", "arch", "temporal_target", "opencode_target"), [
    ("Linux", "x86_64", "linux_amd64.tar.gz", "linux-x64-baseline.tar.gz"),
    ("Linux", "aarch64", "linux_arm64.tar.gz", "linux-arm64.tar.gz"),
    ("Darwin", "x86_64", "darwin_amd64.tar.gz", "darwin-x64-baseline.zip"),
    ("Darwin", "arm64", "darwin_arm64.tar.gz", "darwin-arm64.zip"),
    ("Windows", "AMD64", "windows_amd64.zip", "windows-x64-baseline.zip"),
    ("Windows", "ARM64", "windows_arm64.zip", "windows-arm64.zip"),
])
def test_source_release_assets(tmp_path, system, arch, temporal_target, opencode_target):
    temporal, opencode = portal_source.releases(tmp_path, system, arch)
    assert temporal.url.endswith(f"_{temporal_target}")
    assert opencode.url.endswith(f"/opencode-{opencode_target}")
    suffix = ".exe" if system == "Windows" else ""
    assert temporal.binary == tmp_path / f"temporal{suffix}"
    assert opencode.binary.parent.name == SOURCE_OPENCODE_VERSION
    assert opencode.binary.name == f"opencode{suffix}"
    assert all(len(bytes.fromhex(release.digest)) == 32 for release in (temporal, opencode))


@pytest.mark.parametrize("extension", ["tar.gz", "zip"])
def test_installer_verifies_before_publishing_binary(tmp_path, monkeypatch, extension):
    archive = io.BytesIO()
    if extension == "zip":
        with ZipFile(archive, "w") as zipped:
            zipped.writestr("temporal", b"executable")
    else:
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            binary = tarfile.TarInfo("./temporal")
            binary.size = len(b"executable")
            tar.addfile(binary, io.BytesIO(b"executable"))
    content = archive.getvalue()
    monkeypatch.setattr(portal_source, "urlopen", lambda *args, **kwargs: io.BytesIO(content))
    path = tmp_path / "temporal"
    url = f"https://example.invalid/temporal.{extension}"
    with pytest.raises(ValueError, match="SHA-256"):
        portal_source.install(portal_source.Release(url, "0" * 64, path))
    assert not path.exists()
    release = portal_source.Release(url, hashlib.sha256(content).hexdigest(), path)
    portal_source.install(release)
    assert os.access(path, os.X_OK)
    assert path.read_bytes() == b"executable"
    monkeypatch.setattr(portal_source, "urlopen", None)
    portal_source.install(release)


def source_settings(tmp_path: Path, **fields) -> Settings:
    return Settings(
        _env_file=None, sandbox_driver="local", data_dir=tmp_path,
        model_gateway_url="http://127.0.0.1:4000", agent_backend="opencode", **fields,
    )


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
    path.chmod(0o700)
    return path


def host_binary(path: Path) -> Path:
    return executable(path.with_suffix(".exe") if sys.platform == "win32" else path)


def dsh_fixture(settings: Settings) -> Path:
    modules = dsh_install_dir(settings) / "node_modules"
    package = modules / DSH_PACKAGE
    package.mkdir(parents=True, exist_ok=True)
    (package / "package.json").write_text(
        json.dumps({"version": settings.dsh_version, "bin": {"dsh": "lib/bin.js"}}), encoding="utf-8",
    )
    return executable(package / "lib" / "bin.js" if sys.platform == "win32" else modules / ".bin/dsh")


@pytest.mark.parametrize("cached", [False, True])
def test_dsh_installs_requested_version_once(tmp_path, monkeypatch, cached):
    from sca import host

    data = tmp_path / ".data"
    monkeypatch.setenv("SCA_DSH_VERSION", "0.2.2-rc.10")
    settings = source_settings(data, dsh_version="0.2.2-rc.10")
    prefix = dsh_install_dir(settings)
    if cached:
        dsh_fixture(settings)
    npm = tmp_path / "npm.cmd"
    executable(tmp_path / "node_modules/npm/bin/npm-cli.js")
    monkeypatch.setattr(host, "host_executable", lambda name: npm if name.startswith("npm") else tmp_path / "node")
    calls = []

    def install(command, log, **kwargs):
        calls.append(command)
        assert command[command.index("--prefix") + 1] == prefix
        if "install" in command:
            assert "--ignore-scripts" in command
            assert command[-1] == "@deepseek-ai/dsh@0.2.2-rc.10"
            dsh_fixture(settings)
        else:
            assert "rebuild" in command
            assert command[-3:] == ["@deepseek-ai/dsh-subprocess-local", "koffi", "node-pty"]
            assert json.loads((prefix / "package.json").read_text(encoding="utf-8"))["allowScripts"] == {
                "@deepseek-ai/dsh-subprocess-local": True, "koffi": True, "node-pty": True,
                "@google/genai": False, "protobufjs": False,
            }

    monkeypatch.setattr(portal_source, "run_logged", install)
    portal_source.install_dsh(data)
    portal_source.install_dsh(data)
    first, second = calls
    assert "install" in first and "rebuild" in second
    assert installed_dsh(settings) is not None
    assert installed_dsh(settings.model_copy(update={"dsh_version": "0.2.2"})) is None


def test_install_reports_missing_npm(tmp_path, monkeypatch):
    from sca import host

    monkeypatch.setattr(host, "host_executable", lambda _: None)
    with pytest.raises(ValueError, match="安装 DeepSeek Harness 需要 npm"):
        portal_source.install_dsh(tmp_path)


def test_postgres_portable_archive_is_verified(tmp_path, monkeypatch):
    archive = io.BytesIO()
    with ZipFile(archive, "w") as zipped:
        for name in ("initdb", "pg_ctl", "postgres", "psql"):
            zipped.writestr(f"pgsql/bin/{name}.exe", name.encode("ascii"))
        zipped.writestr("pgsql/share/postgresql.conf.sample", b"")
    content = archive.getvalue()
    checksum = b"0" * 64

    def fetch(url, **kwargs):
        return io.BytesIO(checksum if url.endswith(".sha256") else content)

    monkeypatch.setattr(portal_source, "urlopen", fetch)
    with pytest.raises(ValueError, match="校验"):
        portal_source.install_postgres(tmp_path)
    assert not list(tmp_path.iterdir())
    checksum = hashlib.sha256(content).hexdigest().encode("ascii")
    tools = portal_source.install_postgres(tmp_path)
    assert portal_source.pg_tools_present(tools)
    assert (tools.parent / "share/postgresql.conf.sample").is_file()


@pytest.mark.parametrize("initialized", [False, True])
def test_windows_postgres_grants_access_and_uses_a_dedicated_server_log(tmp_path, monkeypatch, initialized):
    stack = portal_source.WindowsSource(tmp_path / "repo with spaces")
    stack.runtime.mkdir(parents=True)
    bin_dir = tmp_path / "PostgreSQL Tools" / "bin"
    server_log = stack.runtime / "postgres-server.log"
    if initialized:
        stack.pg.mkdir()
        (stack.pg / "PG_VERSION").write_text("17\n", encoding="utf-8")
        portal_source.write_password(stack.pg_password)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    sid = portal_source.windows_user_sid() if sys.platform == "win32" else "S-1-5-21-123-456-789-1001"
    monkeypatch.setattr(portal_source, "windows_user_sid", lambda: sid)
    monkeypatch.setattr(portal_source, "select_port", lambda port, strict: 55432)
    monkeypatch.setattr(stack, "find_postgres", lambda *, download: bin_dir)
    monkeypatch.setattr(stack, "remember", lambda name, pid: None)
    calls = []

    def run(argv, log, **kwargs):
        program = Path(argv[0]).stem
        calls.append(program)
        assert log == stack.runtime / "postgres.log"
        if program == "icacls":
            path, acl = {
                1: (stack.pg, "(OI)(CI)F"),
                2: (stack.pg_password, "F"),
                3: (server_log, "F"),
            }[len(calls)]
            assert path.exists()
            assert argv[1:] == [path, "/grant", f"*{sid}:{acl}", "/Q"]
        elif program == "initdb":
            assert "--auth=scram-sha-256" in argv
            assert argv[argv.index("--pwfile") + 1] == stack.pg_password
        elif program == "pg_ctl":
            assert argv[argv.index("-l") + 1] == server_log
            assert argv[argv.index("-o") + 1] == "-h 127.0.0.1 -p 55432"
            assert "-w" in argv and kwargs["cwd"] == stack.pg
            (stack.pg / "postmaster.pid").write_text("123\ndata\n0\n55432\n", encoding="utf-8")
        elif program == "psql":
            assert argv[argv.index("-p") + 1] == "55432"
            assert "CREATE DATABASE sca" in kwargs["input_text"]
            assert "CREATE DATABASE litellm" in kwargs["input_text"]

    monkeypatch.setattr(portal_source, "run_logged", run)
    stack.start_postgres()
    assert calls == ["icacls"] * 3 + ([] if initialized else ["initdb"]) + ["pg_ctl", "psql"]
    assert stack.pg_port.read_text(encoding="utf-8").strip() == "55432"


def test_windows_configuration_persists_token_and_defaults_to_dsh(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SCA_AGENT_BACKEND", raising=False)
    monkeypatch.delenv("SCA_DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("SCA_PORTAL_REQUIRE_TOKEN", raising=False)
    monkeypatch.setenv("PORTAL_TOKEN", "local-token-fixture")
    stack = portal_source.WindowsSource(tmp_path)
    stack.runtime.mkdir(parents=True)
    (tmp_path / ".env.example").write_text("DEEPSEEK_API_KEY=REPLACE_ME\n", encoding="utf-8")
    config = stack.set_configuration()
    assert config["SCA_AGENT_BACKEND"] == "dsh"
    assert config["SCA_DEFAULT_MODEL"] == "deepseek-flash"
    assert config["SCA_API_TOKENS"] == "local-token-fixture:eval:admin"
    assert config["SCA_PORTAL_REQUIRE_TOKEN"] == "0"
    monkeypatch.delenv("PORTAL_TOKEN")
    assert stack.set_configuration() == config
    monkeypatch.setenv("SCA_PORTAL_REQUIRE_TOKEN", "true")
    assert stack.set_configuration()["SCA_PORTAL_REQUIRE_TOKEN"] == "1"
    monkeypatch.delenv("SCA_PORTAL_REQUIRE_TOKEN")
    assert stack.set_configuration()["SCA_PORTAL_REQUIRE_TOKEN"] == "1"
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("migration_fails", [False, True])
def test_windows_up_migrates_before_starting_api_and_worker(tmp_path, monkeypatch, migration_fails):
    from sca import host

    stack = portal_source.WindowsSource(tmp_path)
    starts = []
    env = {"SCA_TEMPORAL_ADDRESS": "127.0.0.1:7233", "SCA_SANDBOX_DRIVER": "local"}
    monkeypatch.setattr(host, "host_executable", lambda name: tmp_path / name)
    monkeypatch.setattr(host, "windows_bash", lambda: tmp_path / "bash.exe")
    monkeypatch.setattr(portal_source.subprocess, "check_output", lambda *args, **kwargs: "v24.22.3")
    for step in ("install_binaries", "install_dsh", "prepare_gateway"):
        monkeypatch.setattr(portal_source, step, lambda *args, **kwargs: None)
    monkeypatch.setattr(stack, "set_configuration", lambda: {"SCA_AGENT_BACKEND": "fake"})
    monkeypatch.setattr(stack, "environment", lambda config: env)
    monkeypatch.setattr(stack, "start_postgres", lambda: starts.append("postgres"))
    monkeypatch.setattr(stack, "start_gateway", lambda env: starts.append("gateway"))
    def start(name, argv, *args):
        if name == "api":
            assert argv == [stack.python, "-m", "sca.api.portal_server", "--no-proxy-headers",
                            "--host", "127.0.0.1", "--port", "8000"]
        starts.append(name)

    monkeypatch.setattr(stack, "start", start)
    monkeypatch.setattr(stack, "wait", lambda *args: None)
    monkeypatch.setattr(stack, "owned", lambda service: 1)
    stack.data.mkdir()

    def migration(argv, log, **kwargs):
        assert argv == [stack.python, "-m", "alembic", "upgrade", "head"]
        assert kwargs["env"] == env and log.name == "migrations.log"
        starts.append("migration")
        if migration_fails:
            raise ValueError("fixture migration error")

    monkeypatch.setattr(portal_source, "run_logged", migration)
    if migration_fails:
        with pytest.raises(ValueError, match="fixture migration error"):
            stack.up()
        assert starts == ["postgres", "migration"]
    else:
        stack.up()
        assert starts == ["postgres", "migration", "temporal", "gateway", "worker", "api"]


@pytest.mark.parametrize("action", ["import", "export"])
def test_windows_history_uses_source_database_and_preserves_archive_path(tmp_path, monkeypatch, action):
    repo = tmp_path / "checkout with spaces"
    script = repo / "scripts/portal_source.py"
    venv = repo / ".venv"
    archive = tmp_path / "历史 bundle.zip"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [str(script), f"{action}-history", archive.name])
    monkeypatch.setattr(portal_source, "__file__", str(script))
    monkeypatch.setattr(portal_source, "sys", SimpleNamespace(
        platform="win32", version_info=sys.version_info, prefix=str(venv),
        executable=sys.executable,
    ))
    monkeypatch.setenv("SCA_DATABASE_URL", "postgresql+asyncpg://fixture.invalid/unrelated")
    monkeypatch.setenv("SCA_DATA_DIR", str(tmp_path / "unrelated"))
    stack = portal_source.WindowsSource(repo)
    stack.python.parent.mkdir(parents=True)
    stack.python.touch()
    manifest = b'[project]\nname = "fixture"\n'
    (repo / "pyproject.toml").write_bytes(manifest)
    (venv / ".sca-source-project").write_text(hashlib.sha256(manifest).hexdigest(), encoding="ascii")
    calls = []

    def start_postgres(stack):
        calls.append("postgres")
        stack.pg_port.write_text("55432\n", encoding="ascii")
        stack.pg_password.write_text("fixture-password\n", encoding="ascii")

    def transfer(argv, *, cwd, env):
        calls.append("history")
        assert argv == [stack.python, script, "history", action, str(archive), "--migrate"]
        assert cwd == repo
        worker_env = stack.environment({})
        for key in ("SCA_DATABASE_URL", "SCA_DATA_DIR", "SCA_SANDBOX_DRIVER"):
            assert env[key] == worker_env[key]
        assert env["SCA_DATABASE_URL"].endswith("@127.0.0.1:55432/sca")
        assert env["SCA_DATA_DIR"] == str(stack.data)
        return 7

    monkeypatch.setattr(portal_source.WindowsSource, "start_postgres", start_postgres)
    monkeypatch.setattr(portal_source.subprocess, "call", transfer)
    with pytest.raises(SystemExit) as error:
        portal_source.main()
    assert error.value.code == 7
    assert calls == ["postgres", "history"]


def test_windows_history_prepares_venv_and_forwards_archive_on_first_run(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "pyproject.toml").touch()
    script = repo / "scripts/portal_source.py"
    python = repo / ".venv/Scripts/python.exe"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(portal_source, "__file__", str(script))
    monkeypatch.setattr(portal_source, "sys", SimpleNamespace(
        platform="win32", version_info=sys.version_info, prefix=sys.prefix, executable=sys.executable,
    ))
    calls = []

    def install(argv, log):
        calls.append(argv[:3])
        assert log.name == "setup.log" and log.parent.is_dir()
        if argv[2] == "venv":
            python.parent.mkdir(parents=True)
            python.touch()

    def launch(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr(portal_source, "run_logged", install)
    monkeypatch.setattr(portal_source.subprocess, "call", launch)
    with pytest.raises(SystemExit) as error:
        portal_source.manage_windows("import-history", archive=Path("history copy.zip"))
    assert error.value.code == 0
    assert calls == [
        [sys.executable, "-m", "venv"], [python, "-m", "pip"],
        [python, "-X", "utf8", script, "import-history", str(tmp_path / "history copy.zip")],
    ]


@pytest.mark.parametrize("args", [["import-history"], ["export-history", "one.zip", "two.zip"]])
def test_windows_history_requires_one_archive(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["portal_source.py", *args])
    monkeypatch.setattr(portal_source, "manage_windows", None)
    with pytest.raises(SystemExit) as error:
        portal_source.main()
    assert error.value.code == 2


@pytest.mark.parametrize("mode", ["source", "docker"])
def test_history_cli_uses_worker_storage_settings(tmp_path, monkeypatch, mode):
    from sca import config

    monkeypatch.chdir(tmp_path)
    for name in ("SCA_DATA_DIR", "SCA_OBJECT_STORE", "SCA_S3_BUCKET", "SCA_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "SCA_DATABASE_URL=postgresql+asyncpg://fixture.invalid/portal\n"
        "SCA_DATA_DIR=portal-data\nSCA_OBJECT_STORE=s3\nSCA_S3_BUCKET=fixture-bucket\n",
        encoding="utf-8",
    )
    if mode == "source":
        monkeypatch.setenv("SCA_DATABASE_URL", "postgresql+asyncpg://127.0.0.1:55432/sca")
        monkeypatch.setenv("SCA_DATA_DIR", str(tmp_path / ".data"))
        monkeypatch.setenv("SCA_OBJECT_STORE", "local")
    worker = Settings()
    monkeypatch.setattr(config, "get_settings", Settings)
    calls = []

    def record(argv, *, env, **kwargs):
        calls.append(argv)
        assert env["SCA_DATABASE_URL"] == worker.database_url
        assert Path(env["SCA_DATA_DIR"]) == worker.data_dir
        assert env["SCA_OBJECT_STORE"] == worker.object_store
        if mode == "docker":
            assert env["SCA_S3_BUCKET"] == worker.s3_bucket
        if argv[2] == "alembic":
            assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        return 0

    monkeypatch.setattr(portal_source.subprocess, "run", record)
    monkeypatch.setattr(portal_source.subprocess, "call", record)
    archive = tmp_path / "history copy.zip"
    if mode == "source":
        assert portal_source.transfer_history("import", archive, migrate=True) == 0
        assert calls == [
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            [sys.executable, "-m", "sca.history", "import", str(archive)],
        ]
    else:
        assert portal_source.transfer_history("export", archive, migrate=False) == 0
        assert calls == [[sys.executable, "-m", "sca.history", "export", "--out", str(archive)]]


def test_history_migration_failure_blocks_import_and_hides_details(tmp_path, monkeypatch):
    from sca import config

    monkeypatch.setattr(config, "get_settings", lambda: Settings(_env_file=None))

    def failed_migration(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="fixture-private-details")

    monkeypatch.setattr(portal_source.subprocess, "run", failed_migration)
    monkeypatch.setattr(portal_source.subprocess, "call", None)
    with pytest.raises(ValueError, match="历史数据库迁移失败") as error:
        portal_source.transfer_history("import", tmp_path / "archive.zip", migrate=True)
    assert "fixture-private-details" not in str(error.value)


def test_settings_default_dsh_for_windows_local_only(monkeypatch):
    from sca import config

    monkeypatch.setattr(config, "sys", SimpleNamespace(platform="win32"))
    assert Settings(_env_file=None, sandbox_driver="local").agent_backend == "dsh"
    assert Settings(_env_file=None, sandbox_driver="local").default_model == "deepseek-flash"
    assert Settings(_env_file=None, sandbox_driver="docker").agent_backend == "codex"
    assert Settings(_env_file=None, sandbox_driver="local", agent_backend="fake").agent_backend == "fake"


def test_file_lock_can_retry_after_contention(tmp_path):
    from sca.host import lock_file, unlock_file

    path = tmp_path / "lock"
    with path.open("a+b") as first, path.open("a+b") as second:
        lock_file(first)
        with pytest.raises(BlockingIOError):
            lock_file(second, blocking=False)
        unlock_file(first)
        lock_file(second, blocking=False)
        unlock_file(second)


@pytest.mark.parametrize("require_token", [False, True])
def test_source_smoke_uses_fake_and_checks_patch(tmp_path, require_token):
    from uuid import uuid4

    task_id = uuid4()
    task = f"/v1/tasks/{task_id}"
    origin = "http://127.0.0.1:8000"

    def respond(request):
        match request.url.path:
            case "/v1/portal/config":
                return httpx.Response(200, json={"sandbox_driver": "local",
                    "gateway_reachable": True, "temporal_reachable": True})
            case "/v1/portal/access":
                return httpx.Response(200, json={"require_token": require_token,
                                                 "local_no_token_active": not require_token})
            case "/portal":
                assert "Authorization" not in request.headers
                return httpx.Response(200, html="<title>SuperCloudAgents</title>")
            case "/v1/tasks":
                assert request.headers.get("Authorization") == ("Bearer fixture" if require_token else None)
                assert request.headers.get("Origin") == (None if require_token else origin)
                spec = json.loads(request.content)["spec"]
                assert spec["agent_backend"] == "fake" and "sca-fake" in spec["instruction"]
                assert spec["auto_finish"] and spec["verify_command"] == "grep -qx ready result.txt"
                assert spec["repo_url"] == (tmp_path / "repo with spaces").as_posix()
                assert (Path(spec["repo_url"]) / "result.txt").read_bytes() == b"before\n"
                return httpx.Response(201, json={"id": str(task_id)})
            case path if path == task:
                return httpx.Response(200, json={"state": "SUCCEEDED", "result": {"verification_passed": True}})
            case path if path == task + "/artifacts/changes.patch":
                return httpx.Response(200, text="-before\n+ready\n")
            case _:
                raise AssertionError(request.url.path)

    with httpx.Client(base_url=origin, transport=httpx.MockTransport(respond),
                      headers={"Authorization": "Bearer fixture"}) as client:
        assert source_smoke.smoke(client, tmp_path / "repo with spaces", 1) == task_id


@pytest.mark.parametrize(("driver", "tenant", "role", "backend", "allowed"), [
    ("local", "eval", "admin", "fake", True),
    ("local", "eval", "member", "fake", False),
    ("local", "default", "admin", "fake", False),
    ("local", "eval", "admin", "dsh", False),
    ("docker", "eval", "admin", "fake", False),
])
async def test_model_free_submission_requires_local_demo_admin(
    tmp_path, monkeypatch, driver, tenant, role, backend, allowed,
):
    from sca.api.model_access import ModelNotConfiguredError
    from sca.api.tasks_admission import Submission, submit

    config = tmp_path / "demo-config"
    config.write_text("DEEPSEEK_API_KEY=REPLACE_ME\n", encoding="utf-8")
    monkeypatch.setenv("SCA_PORTAL_LOCAL_CONFIG", str(config))
    spec = {"repo_url": "repo", "instruction": "example", "agent_backend": backend}
    task = SimpleNamespace(id="task", kind="coding", spec=spec)
    store = SimpleNamespace(create_task=AsyncMock(return_value=(task, False)))
    start = AsyncMock()
    state = SimpleNamespace(
        settings=Settings(_env_file=None, sandbox_driver=driver),
        store=SimpleNamespace(for_tenant=lambda _: store),
        orchestrator=SimpleNamespace(start_task=start),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    user = Principal(tenant_id=tenant, role=role, subject="fixture")
    body = Submission(kind="coding", spec=spec)
    if allowed:
        assert await submit(body, request, user) == task
        start.assert_awaited_once_with(task.id, task.kind)
    else:
        with pytest.raises(ModelNotConfiguredError):
            await submit(body, request, user)
        store.create_task.assert_not_awaited()
        start.assert_not_awaited()


async def test_worker_uses_local_sandbox_and_loopback_gateway(tmp_path):
    settings = source_settings(tmp_path)
    sandbox = make_sandbox_provider(settings)
    assert isinstance(sandbox, LocalSandboxProvider)
    assert settings.state_transport == "sync"
    assert sandbox.root == tmp_path / "sandboxes"
    gateway = await make_gateway(settings, sandbox)
    try:
        assert gateway.sandbox_url == "http://127.0.0.1:4000/v1"
    finally:
        await gateway.aclose()


async def test_portal_config_reports_installed_backends_and_warning(tmp_path, monkeypatch):
    settings = source_settings(tmp_path, codex_bin_dir=tmp_path / "tools")
    host_binary(settings.opencode_versions_dir / SOURCE_OPENCODE_VERSION / "opencode")
    codex = host_binary(tmp_path / "tools" / "codex")
    node = host_binary(tmp_path / "tools" / "node")
    dsh = dsh_fixture(settings)
    monkeypatch.setenv("PATH", str(tmp_path / "tools"))
    monkeypatch.setattr(portal, "get_settings", lambda: settings)
    monkeypatch.setattr(portal, "gateway_models", AsyncMock(return_value=(True, ["deepseek-flash"])))
    monkeypatch.setattr(portal, "temporal_reachable", AsyncMock(return_value=True))
    user = Principal(tenant_id="eval", subject="demo")
    request = Request({"type": "http", "client": ("127.0.0.1", 43120),
                       "app": SimpleNamespace(state=SimpleNamespace())})
    response = await portal.config(request, user)
    assert response["warning"] == SOURCE_MODE_WARNING
    assert response["sandbox_driver"] == "local"
    assert response["agent_backends"] == ["opencode", "codex", "dsh"]
    assert sorted(response["unavailable_agent_backends"]) == ["opencode-brain", "pi"]
    assert SOURCE_MODE_WARNING.encode() in _portal_page(settings).body

    codex_backend = make_agent_backend(settings.model_copy(update={"agent_backend": "codex"}))
    assert codex_backend.executable == str(codex)
    with pytest.raises(FileNotFoundError, match="pi"):
        make_agent_backend(settings.model_copy(update={"agent_backend": "pi"}))
    pi = host_binary(tmp_path / "tools" / "pi")
    assert make_agent_backend(settings.model_copy(update={"agent_backend": "pi"})).executable == str(pi)
    dsh_backend = make_agent_backend(settings.model_copy(update={"agent_backend": "dsh"}))
    assert dsh_backend.executable == str(dsh)
    assert dsh_backend.node_executable == (str(node) if sys.platform == "win32" else None)
    assert dsh_backend.state_transport == "sync"


async def test_upstream_opencode_uses_champion_plugin(tmp_path):
    settings = source_settings(tmp_path)
    binary = host_binary(settings.opencode_versions_dir / SOURCE_OPENCODE_VERSION / "opencode")
    backend = make_agent_backend(settings)
    overlay = AgentOverlay(system_prompt="首选提示", tool_guidance="Use read",
                           tool_descriptions={"read": "Read carefully"})
    ctx = AgentContext(task_id="test", agent_home_host=str(tmp_path / "home"),
                       model_base_url="http://127.0.0.1:4000/v1", model_api_key="virtual",
                       agent_overlay=overlay)
    assert not backend.sandbox_additions(ctx).mounts

    class Sandbox:
        async def exec(self, sandbox_id, argv, *, env, **kwargs):
            assert argv[0] == str(binary)
            config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            plugin, = config["plugins"]
            assert Path(plugin["package"], "index.js").is_file()
            overlay_file = Path(plugin["options"]["path"])
            assert json.loads(overlay_file.read_text(encoding="utf-8")) == overlay.model_dump(exclude_none=True)
            yield ExecOutput(stream="stdout", text='{"type":"step_finish","part":{"reason":"stop"}}')
            yield ExecExit(exit_code=0)

    events = [event async for event in backend.run_turn(
        Sandbox(), "sandbox", ctx, "edit", session_id=None, timeout_s=10,
    )]
    assert events[-1].type == "turn_completed"


def test_opencode_runtime_plugin_updates_the_model_request(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    path = Path(__file__).resolve().parents[2] / "src/sca/agents/opencode_overlay/index.js"
    overlay = tmp_path / "overlay.json"
    overlay.write_text(AgentOverlay(
        system_prompt="Revised ${OPENCODE_TOOL_GUIDANCE}", tool_guidance="$& guidance",
        tool_descriptions={"read": "", "shell": "Run command"},
    ).canonical_json())
    result = subprocess.run(
        [node, "--input-type=module", "-e", """
import {readFileSync} from 'node:fs';
const code = readFileSync(process.argv[1], 'utf8');
const {default: plugin} = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
let hook;
const tools = {read: {description: 'Old'}, shell: {description: 'Old'}, write: {description: 'Keep'}};
await plugin.setup({
  options: {path: process.argv[2]},
  tool: {transform: async (callback) => callback({update: (name, update) => update(tools[name])})},
  session: {hook: async (name, callback) => {if (name === 'context') hook = callback;}},
});
const event = {system: [{text: 'Old'}, {text: 'Project guidance'}], tools};
hook(event);
console.log(JSON.stringify(event));
""", str(path), str(overlay)],
        capture_output=True, text=True, check=True, timeout=5,
    )
    event = json.loads(result.stdout)
    assert [part["text"] for part in event["system"]] == ["Revised $& guidance", "Project guidance"]
    assert {name: tool["description"] for name, tool in event["tools"].items()} == {
        "read": "", "shell": "Run command", "write": "Keep",
    }
