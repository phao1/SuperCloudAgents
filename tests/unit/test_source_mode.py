import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import httpx
import pytest

from sca.agents.local import DSH_PACKAGE, dsh_install_dir, installed_dsh
from sca.api import portal
from sca.api.app import _portal_page
from sca.config import SOURCE_MODE_WARNING, SOURCE_OPENCODE_VERSION, Settings
from sca.core.models import AgentContext, AgentOverlay, ExecExit, ExecOutput, Principal
from sca.orchestration.worker import make_agent_backend, make_gateway, make_sandbox_provider
from sca.sandbox.local import LocalSandboxProvider
from scripts import portal_source, source_smoke


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


def test_dsh_installs_requested_version_once(tmp_path, monkeypatch):
    from sca import host

    data = tmp_path / ".data"
    monkeypatch.setenv("SCA_DSH_VERSION", "0.2.2-rc.10")
    settings = source_settings(data, dsh_version="0.2.2-rc.10")
    npm = tmp_path / "npm.cmd"
    executable(tmp_path / "node_modules/npm/bin/npm-cli.js")
    monkeypatch.setattr(host, "host_executable", lambda name: npm if name.startswith("npm") else tmp_path / "node")
    calls = []

    def install(command, log, **kwargs):
        calls.append(command)
        assert command[command.index("--prefix") + 1] == dsh_install_dir(settings)
        assert command[-1] == "@deepseek-ai/dsh@0.2.2-rc.10"
        dsh_fixture(settings)

    monkeypatch.setattr(portal_source, "run_logged", install)
    portal_source.install_dsh(data)
    portal_source.install_dsh(data)
    assert len(calls) == 1
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


def test_windows_configuration_persists_token_and_defaults_to_dsh(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SCA_AGENT_BACKEND", raising=False)
    monkeypatch.delenv("SCA_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("PORTAL_TOKEN", "local-token-fixture")
    stack = portal_source.WindowsSource(tmp_path)
    stack.runtime.mkdir(parents=True)
    (tmp_path / ".env.example").write_text("DEEPSEEK_API_KEY=REPLACE_ME\n", encoding="utf-8")
    config = stack.set_configuration()
    assert config["SCA_AGENT_BACKEND"] == "dsh"
    assert config["SCA_DEFAULT_MODEL"] == "deepseek-flash"
    assert config["SCA_API_TOKENS"] == "local-token-fixture:eval:admin"
    monkeypatch.delenv("PORTAL_TOKEN")
    assert stack.set_configuration() == config
    assert capsys.readouterr() == ("", "")


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


def test_source_smoke_uses_fake_and_checks_patch(tmp_path):
    from uuid import uuid4

    task_id = uuid4()
    task = f"/v1/tasks/{task_id}"

    def respond(request):
        match request.url.path:
            case "/v1/portal/config":
                return httpx.Response(200, json={"sandbox_driver": "local",
                    "gateway_reachable": True, "temporal_reachable": True})
            case "/portal":
                return httpx.Response(200, html="<title>SuperCloudAgents</title>")
            case "/v1/tasks":
                spec = json.loads(request.content)["spec"]
                assert spec["agent_backend"] == "fake" and "sca-fake" in spec["instruction"]
                assert spec["auto_finish"] and spec["verify_command"] == "grep -qx ready result.txt"
                assert (Path(spec["repo_url"]) / "result.txt").read_bytes() == b"before\n"
                return httpx.Response(201, json={"id": str(task_id)})
            case path if path == task:
                return httpx.Response(200, json={"state": "SUCCEEDED", "result": {"verification_passed": True}})
            case path if path == task + "/artifacts/changes.patch":
                return httpx.Response(200, text="-before\n+ready\n")
            case _:
                raise AssertionError(request.url.path)

    with httpx.Client(base_url="http://unit", transport=httpx.MockTransport(respond)) as client:
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
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
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
