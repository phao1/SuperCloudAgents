import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest

from sca.api import portal
from sca.api.app import _portal_page
from sca.config import SOURCE_MODE_WARNING, SOURCE_OPENCODE_VERSION, Settings
from sca.core.models import AgentContext, AgentOverlay, ExecExit, ExecOutput, Principal
from sca.orchestration.worker import make_agent_backend, make_gateway, make_sandbox_provider
from sca.sandbox.local import LocalSandboxProvider
from scripts import portal_source


@pytest.mark.parametrize(("system", "arch", "temporal_target", "opencode_target"), [
    ("Linux", "x86_64", "linux_amd64.tar.gz", "linux-x64-baseline.tar.gz"),
    ("Linux", "aarch64", "linux_arm64.tar.gz", "linux-arm64.tar.gz"),
    ("Darwin", "x86_64", "darwin_amd64.tar.gz", "darwin-x64-baseline.zip"),
    ("Darwin", "arm64", "darwin_arm64.tar.gz", "darwin-arm64.zip"),
])
def test_source_release_assets(tmp_path, system, arch, temporal_target, opencode_target):
    temporal, opencode = portal_source.releases(tmp_path, system, arch)
    assert temporal.url.endswith(f"_{temporal_target}")
    assert opencode.url.endswith(f"/opencode-{opencode_target}")
    assert temporal.binary == tmp_path / "temporal"
    assert opencode.binary.parent.name == SOURCE_OPENCODE_VERSION
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
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


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
    executable(settings.opencode_versions_dir / SOURCE_OPENCODE_VERSION / "opencode")
    codex = executable(tmp_path / "tools" / "codex")
    monkeypatch.setenv("PATH", str(tmp_path / "tools"))
    monkeypatch.setattr(portal, "get_settings", lambda: settings)
    monkeypatch.setattr(portal, "gateway_models", AsyncMock(return_value=(True, ["deepseek-flash"])))
    monkeypatch.setattr(portal, "temporal_reachable", AsyncMock(return_value=True))
    user = Principal(tenant_id="eval", subject="demo")
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    response = await portal.config(request, user)
    assert response["warning"] == SOURCE_MODE_WARNING
    assert response["sandbox_driver"] == "local"
    assert response["agent_backends"] == ["opencode", "codex"]
    assert sorted(response["unavailable_agent_backends"]) == ["opencode-brain", "pi"]
    assert SOURCE_MODE_WARNING.encode() in _portal_page(settings).body

    codex_backend = make_agent_backend(settings.model_copy(update={"agent_backend": "codex"}))
    assert codex_backend.executable == str(codex)
    with pytest.raises(FileNotFoundError, match="pi"):
        make_agent_backend(settings.model_copy(update={"agent_backend": "pi"}))
    pi = executable(tmp_path / "tools" / "pi")
    assert make_agent_backend(settings.model_copy(update={"agent_backend": "pi"})).executable == str(pi)


async def test_upstream_opencode_uses_champion_plugin(tmp_path):
    settings = source_settings(tmp_path)
    binary = executable(settings.opencode_versions_dir / SOURCE_OPENCODE_VERSION / "opencode")
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
            assert json.loads(overlay_file.read_text()) == overlay.model_dump(exclude_none=True)
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
