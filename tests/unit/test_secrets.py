from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import hvac
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from sca.config import Settings
from sca.core.models import RepoCredential
from sca.secrets import OpenBaoSecretProvider, redact
from sca.workspace.cache import RepoCache
from sca.workspace.git import GitWorkspace


async def test_paths():
    read = Mock(
        side_effect=[
            hvac.exceptions.InvalidPath(),
            hvac.exceptions.InvalidPath(),
            {"data": {"data": {"type": "token", "username": "git", "token": "secret"}}},
        ]
    )
    client = SimpleNamespace(
        secrets=SimpleNamespace(kv=SimpleNamespace(v2=SimpleNamespace(read_secret_version=read)))
    )
    provider = OpenBaoSecretProvider(Settings(), client=client)
    assert (
        await provider.repo_credential("a", "https://git.test/org/repo.git")
    ).password == "secret"
    assert [c.kwargs["path"] for c in read.call_args_list] == [
        "sca/tenants/a/repos/git.test/org/repo",
        "sca/tenants/a/repos/git.test/org",
        "sca/tenants/a/repos/git.test",
    ]


async def test_app_exchange_and_cache():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    entry = {"type": "github_app", "app_id": "42", "installation_id": "7", "private_key": private}
    read = Mock(return_value={"data": {"data": entry}})
    client = SimpleNamespace(
        secrets=SimpleNamespace(kv=SimpleNamespace(v2=SimpleNamespace(read_secret_version=read)))
    )
    requests = []

    def exchange(request):
        requests.append(request)
        claims = jwt.decode(
            request.headers["Authorization"].split()[1], key.public_key(), algorithms=["RS256"]
        )
        assert claims["iss"] == "42"
        assert 0 < claims["exp"] - claims["iat"] <= 600
        assert request.method == "POST"
        assert str(request.url) == "https://api.github.com/app/installations/7/access_tokens"
        return httpx.Response(
            201,
            json={
                "token": "installation-token",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as http:
        provider = OpenBaoSecretProvider(Settings(), client=client, http=http)
        a = await provider.repo_credential("a", "https://github.com/org/repo")
        assert await provider.repo_credential("a", "https://github.com/org/repo") == a
        assert len(requests) == read.call_count == 1
        a.expires_at = datetime.now(UTC) + timedelta(minutes=4)
        renewed = await provider.repo_credential("a", "https://github.com/org/repo")
        assert renewed.expires_at > a.expires_at
    assert len(requests) == read.call_count == 2


@pytest.mark.parametrize(
    ("prompt", "expected"), [("Username for remote", "user"), ("Password for remote", "secret")]
)
async def test_askpass(prompt, expected):
    import asyncio

    from sca.secrets import git_env

    env = git_env(RepoCredential(username="user", password="secret"))
    proc = await asyncio.create_subprocess_exec(
        env["GIT_ASKPASS"], prompt, env=env, stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    assert out.decode().strip() == expected


async def test_git_environment_and_redaction(tmp_path, monkeypatch):
    import asyncio

    credential = RepoCredential(username="user", password="super-secret")
    calls = []

    class Process:
        returncode = 1

        async def communicate(self):
            return b"", b"https://user:super-secret@git.test error super-secret"

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    from sca.core.errors import WorkspaceError

    with pytest.raises(WorkspaceError) as error:
        await GitWorkspace(tmp_path, "branch").push("https://git.test/org/repo", credential)
    assert "super-secret" not in str(error.value)
    args, kwargs = calls[0]
    assert "super-secret" not in str(args)
    assert "credential.helper=" in args
    assert kwargs["env"]["SCA_GIT_PASSWORD"] == "super-secret"
    assert RepoCache(tmp_path / "cache")._path("url", "a") != RepoCache(tmp_path / "cache")._path(
        "url", "b"
    )
    assert "super-secret" not in redact("super-secret", credential)


async def test_missing_secret_and_host():
    from sca.secrets import HostGitCredentials

    assert await HostGitCredentials().repo_credential("a", "https://git.test/org/repo") is None
    read = Mock(side_effect=hvac.exceptions.InvalidPath())
    client = SimpleNamespace(
        secrets=SimpleNamespace(kv=SimpleNamespace(v2=SimpleNamespace(read_secret_version=read)))
    )
    assert (
        await OpenBaoSecretProvider(Settings(), client=client).repo_credential(
            "a", "https://git.test/org/repo"
        )
        is None
    )
    assert read.call_count == 3


async def test_secret_errors_are_sanitized():
    from sca.core.errors import WorkspaceError

    read = Mock(side_effect=hvac.exceptions.Forbidden("sensitive-body"))
    client = SimpleNamespace(
        secrets=SimpleNamespace(kv=SimpleNamespace(v2=SimpleNamespace(read_secret_version=read)))
    )
    with pytest.raises(WorkspaceError) as error:
        await OpenBaoSecretProvider(Settings(), client=client).repo_credential(
            "a", "https://git.test/org/repo"
        )
    assert "sensitive-body" not in str(error.value)
    assert error.value.__suppress_context__
