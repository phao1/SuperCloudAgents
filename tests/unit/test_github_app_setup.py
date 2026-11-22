from __future__ import annotations

import html
import importlib.util
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "github_app_setup", Path(__file__).resolve().parents[2] / "scripts/github_app_setup.py"
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def test_manifest(tmp_path, capsys):
    path = setup.write_manifest(tmp_path / "app", "alice", "alice/private", 'sca-"dev')
    document = path.read_text()
    payload = json.loads(html.unescape(re.search(r'name="manifest" value="([^"]*)"', document)[1]))
    assert payload == setup.manifest("alice", "alice/private", 'sca-"dev')
    assert payload["public"] is False
    assert payload["hook_attributes"]["active"] is False
    assert payload["default_permissions"] == {
        "contents": "write",
        "metadata": "read",
        "pull_requests": "write",
    }
    assert payload["redirect_url"] == "https://example.invalid/"
    assert 'method="post"' in document
    assert 'action="https://github.com/settings/apps/new"' in document
    assert "document.forms[0].submit()" in document
    assert "manifest=%7B" in capsys.readouterr().out


@pytest.mark.skipif(os.name == "nt", reason="Creating this symlink requires Developer Mode")
def test_save_without_nofollow_preserves_link_target(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "os", SimpleNamespace(fdopen=os.fdopen, replace=os.replace))
    original = tmp_path / "outside"
    original.write_text("original")
    directory = tmp_path / "app"
    setup.save(directory, "target.json", "fixture")
    assert (directory / "target.json").read_text() == "fixture"
    (directory / "private-key.pem").symlink_to(original)
    with pytest.raises(ValueError, match="links or junctions"):
        setup.save(directory, "private-key.pem", "replacement")
    assert original.read_text() == "original"


async def test_convert(tmp_path, capsys):
    directory = tmp_path / "app"
    setup.save(directory, "installation_id", "old")
    (directory / "private-key.pem").write_text("old")
    (directory / "private-key.pem").chmod(0o644)

    def respond(request):
        assert request.method == "POST"
        assert request.url.path == "/app-manifests/one-time-code/conversions"
        return httpx.Response(
            201,
            json={
                "id": 42,
                "slug": "sca-dev",
                "client_id": "client",
                "pem": "test-private-key",
                "client_secret": "discard-me",
                "webhook_secret": "discard-me",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        url = await setup.convert(http, directory, "one-time-code")
    assert url == "https://github.com/apps/sca-dev/installations/new"
    assert json.loads((directory / "app.json").read_text()) == {
        "app_id": 42,
        "slug": "sca-dev",
        "client_id": "client",
    }
    assert (directory / "private-key.pem").read_text() == "test-private-key"
    assert not (directory / "installation_id").exists()
    if os.name != "nt":
        assert directory.stat().st_mode & 0o777 == 0o700
    for path in directory.iterdir():
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
        assert "discard-me" not in path.read_text()
    assert "test-private-key" not in capsys.readouterr().out


async def test_convert_failure_does_not_save(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(422, json={"message": "bad code"})
        )
    ) as http:
        with pytest.raises(httpx.HTTPStatusError):
            await setup.convert(http, tmp_path / "app", "bad")
    assert not (tmp_path / "app").exists()


async def test_status_selects_target_installation(tmp_path, monkeypatch, capsys):
    import sca.secrets

    setup.save(tmp_path, "app.json", json.dumps({"app_id": 42}))
    setup.save(tmp_path, "private-key.pem", "test-key")
    setup.save(tmp_path, "target.json", json.dumps({"repo": "alice/private"}))
    monkeypatch.setattr(sca.secrets, "github_app_jwt", lambda data: "signed-jwt")

    installations = [{"id": 7}]

    def respond(request):
        path = request.url.path
        if path == "/app/installations":
            assert request.headers["Authorization"] == "Bearer signed-jwt"
            return httpx.Response(200, json=installations)
        if path.endswith("/access_tokens"):
            return httpx.Response(
                201, json={"token": "installation-secret", "expires_at": "2099-01-01T00:00:00Z"}
            )
        assert path == "/installation/repositories"
        assert request.headers["Authorization"] == "Bearer installation-secret"
        return httpx.Response(200, json={"repositories": [{"full_name": "alice/private"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        await setup.status(http, tmp_path)
    assert (tmp_path / "installation_id").read_text() == "7"
    if os.name != "nt":
        assert (tmp_path / "installation_id").stat().st_mode & 0o777 == 0o600
    installations.append({"id": 8})
    # Multiple installations granting the target must not silently select one.
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ValueError, match="Install the app"):
            await setup.status(http, tmp_path)
    assert not (tmp_path / "installation_id").exists()
    assert "installation-secret" not in capsys.readouterr().out


def test_store_tenant_path(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import sca.secrets

    setup.save(tmp_path, "target.json", json.dumps({"repo": "alice/private"}))
    setup.save(tmp_path, "app.json", json.dumps({"app_id": 42}))
    setup.save(tmp_path, "private-key.pem", "test-key")
    setup.save(tmp_path, "installation_id", "7")
    write = Mock()
    provider = SimpleNamespace(
        prefix="sca/tenants",
        mount="secret",
        client=SimpleNamespace(
            secrets=SimpleNamespace(
                kv=SimpleNamespace(v2=SimpleNamespace(create_or_update_secret=write))
            )
        ),
    )
    monkeypatch.setattr(sca.secrets, "OpenBaoSecretProvider", lambda settings: provider)
    assert setup.store(tmp_path, "team") == "sca/tenants/team/repos/github.com/alice/private"
    write.assert_called_once_with(
        path="sca/tenants/team/repos/github.com/alice/private",
        mount_point="secret",
        secret={
            "type": "github_app",
            "app_id": 42,
            "private_key": "test-key",
            "installation_id": "7",
        },
    )
