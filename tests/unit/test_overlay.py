import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sca.core.errors import WorkspaceError
from sca.core.models import Mount, SandboxSpec
from sca.sandbox.docker import DockerSandboxProvider
from sca.workspace.overlay import OverlayWorkspace, helper_argv, volume_name
from sca.workspace.transport import MountTransport


def test_helper_is_networkless_and_has_no_host_binds():
    args = helper_argv("sca-ws-task", "sca-sandbox:latest", ["git", "status"])
    assert "--network=none" in args
    assert args[args.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert args[args.index("--volume") + 1] == "sca-ws-task:/w"
    assert args[-3:] == ["git", "sca-sandbox:latest", "status"]
    with pytest.raises(WorkspaceError):
        helper_argv("/host", "image", ["git"])


def test_transport_selects_volume(tmp_path):
    (tmp_path / "workspace.overlay").touch()
    assert MountTransport(None).mounts(tmp_path) == [
        Mount(source=volume_name(tmp_path), target="/workspace"),
        Mount(source=str(tmp_path / "workspace" / ".sca"), target="/workspace/.sca")]


async def test_docker_does_not_resolve_named_volume(tmp_path, monkeypatch):
    provider = DockerSandboxProvider()
    provider._cli = AsyncMock(side_effect=[(1, "", "No such container"), (0, "", "")])
    monkeypatch.chdir(tmp_path)
    await provider.create("test", SandboxSpec(image="image", network="none", mounts=[
        Mount(source="sca-ws-task", target="/workspace")]))
    args = provider._cli.call_args.args
    assert "sca-ws-task:/workspace" in args
    assert not (tmp_path / "sca-ws-task").exists()


async def test_volume_options_and_no_credentials(tmp_path, monkeypatch):
    ws = OverlayWorkspace(tmp_path / "tasks" / "one", "sca/one")
    ws.overlay.mkdir(parents=True)
    lower = ws.layers.root / ("a" * 64)
    lower.mkdir()
    (ws.task_dir / "workspace.overlay").write_text('{"lower": "' + str(lower) + '"}')
    run = AsyncMock(return_value="")
    monkeypatch.setattr("sca.workspace.overlay.command", run)
    await ws.ensure_volume()
    args = run.call_args.args
    assert args[:3] == ("docker", "volume", "create")
    assert (f"o=lowerdir={lower},upperdir={ws.upper},workdir={ws.overlay / 'work'},"
            "redirect_dir=off,index=off,metacopy=off") in args
    assert args[-1] == "sca-ws-one"


def test_bad_task_name():
    with pytest.raises(WorkspaceError):
        volume_name(Path("/tasks/bad:name"))


def test_snapshot_keeps_ignored_files_and_opaque_value(tmp_path):
    import base64
    import io
    import json
    import tarfile

    ws = OverlayWorkspace(tmp_path / "tasks" / "snapshot", "sca/snapshot")
    ws.workspace.mkdir(parents=True)
    ws.upper.mkdir(parents=True)
    (ws.upper / "node_modules").mkdir()
    (ws.upper / "node_modules/cache").write_text("state")
    os.setxattr(ws.upper / "node_modules", "user.overlay.opaque", b"x")
    (ws.task_dir / "workspace.ready").write_text("a" * 40)
    (ws.task_dir / "layer-source.json").write_text('{"repo_key":"hash","tenant_id":"t"}')
    data = json.loads(ws.snapshot())
    assert data["metadata"]["opaque"] == {"node_modules": "x"}
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(data["archive"]))) as tar:
        assert tar.extractfile("./node_modules/cache").read() == b"state"


async def test_restore_rejects_escape_before_docker(tmp_path, monkeypatch):
    import json

    ws = OverlayWorkspace(tmp_path / "tasks" / "restore", "sca/restore")
    run = AsyncMock()
    monkeypatch.setattr("sca.workspace.overlay.command", run)
    with pytest.raises(WorkspaceError, match="snapshot path"):
        await ws.restore_snapshot(json.dumps({"metadata": {
            "whiteouts": ["../escape"], "opaque": {}}}).encode())
    run.assert_not_called()


async def test_layer_overlay_pins_without_copying(tmp_path):
    from sca.workspace.layers import LayerStore, command

    repo = tmp_path / "repo"
    await command("git", "init", "-b", "main", str(repo))
    (repo / "file").write_text("base")
    await command("git", "-C", str(repo), "add", ".")
    await command("git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                  "commit", "-m", "base")
    commit = await command("git", "-C", str(repo), "rev-parse", "HEAD")
    layers = LayerStore(tmp_path / "layers")
    target = tmp_path / "tasks" / "one" / "overlay"
    target.parent.mkdir(parents=True)
    key = await layers.copy(repo, commit, "identity", target, overlay=True)
    assert list(target.iterdir()) == []
    assert (layers.root / key / "file").read_text() == "base"
    assert (layers.root / key / "file").stat().st_mode & 0o200
    assert await layers.prune(0) > 0
    (target.parent / "overlay.layer").unlink()
    target.rmdir()
    # Missing task directories must not release a potentially mounted lower.
    assert await layers.prune(0) > 0


async def test_helper_cleans_named_container_on_failure(tmp_path, monkeypatch):
    ws = OverlayWorkspace(tmp_path / "tasks" / "failure", "sca/test")
    run = AsyncMock(side_effect=[WorkspaceError("failed"), ""])
    monkeypatch.setattr("sca.workspace.overlay.command", run)
    with pytest.raises(WorkspaceError):
        await ws.helper(["git", "status"])
    started = run.call_args_list[0].args
    name = started[started.index("--name") + 1]
    assert run.call_args_list[1].args == ("docker", "rm", "-f", name)


async def test_snapshot_restore_round_trip_with_fake_lifecycle(tmp_path, monkeypatch):
    import shutil

    ws = OverlayWorkspace(tmp_path / "tasks" / "roundtrip", "sca/test")
    ws.upper.mkdir(parents=True)
    ws.workspace.mkdir()
    (ws.upper / "modified").write_text("agent output")
    (ws.workspace / ".sca").mkdir()
    (ws.workspace / ".sca/memory").write_text("context")
    (ws.task_dir / "workspace.ready").write_text("a" * 40)
    (ws.task_dir / "layer-source.json").write_text('{"repo_key":"hash","tenant_id":"t"}')
    snapshot = ws.snapshot()
    (ws.upper / "modified").write_text("later interrupted turn")

    async def clear():
        shutil.rmtree(ws.overlay)

    async def build(commit):
        assert commit == "a" * 40
        ws.upper.mkdir(parents=True)

    monkeypatch.setattr(ws, "clear_upper", clear)
    monkeypatch.setattr(ws, "build_workspace", build)
    monkeypatch.setattr(ws, "remove_volume", AsyncMock())
    monkeypatch.setattr(ws, "ensure_volume", AsyncMock())
    helper = AsyncMock()
    monkeypatch.setattr(ws, "helper", helper)
    await ws.restore_snapshot(snapshot)
    assert (ws.upper / "modified").read_text() == "agent output"
    assert (ws.workspace / ".sca/memory").read_text() == "context"
    compile(helper.call_args.args[0][2], "whiteout-helper", "exec")


def test_embedded_cleanup_scripts_compile():
    from sca.workspace.overlay import CLEANUP_SCRIPT, WHITEOUT_SCRIPT

    compile(CLEANUP_SCRIPT, "cleanup", "exec")
    compile(WHITEOUT_SCRIPT, "whiteouts", "exec")


async def test_configured_binary_for_every_overlay_command(tmp_path, monkeypatch):
    import json

    ws = OverlayWorkspace(tmp_path / "tasks" / "binary", "sca/test",
                          docker_bin="/fake/docker-wrapper")
    ws.overlay.mkdir(parents=True)
    lower = ws.layers.root / ("b" * 64)
    lower.mkdir()
    (ws.task_dir / "workspace.overlay").write_text(json.dumps({"lower": str(lower)}))
    run = AsyncMock(return_value=ws.volume)
    monkeypatch.setattr("sca.workspace.overlay.command", run)
    await ws.helper(["git", "status"])
    await ws.ensure_volume()
    await ws.remove_volume()
    # Fake helper does not remove directories itself.
    await ws.clear_upper()
    assert len(run.call_args_list) == 6
    assert all(call.args[0] == "/fake/docker-wrapper" for call in run.call_args_list)
