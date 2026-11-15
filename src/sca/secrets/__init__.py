from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import hvac
import jwt

from sca.config import Settings
from sca.core.errors import WorkspaceError
from sca.core.interfaces import SecretProvider
from sca.core.models import RepoCredential


def github_app_jwt(data: dict) -> str:
    now = int(datetime.now(UTC).timestamp())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": str(data["app_id"])},
        data["private_key"],
        algorithm="RS256",
    )


class HostGitCredentials:
    async def repo_credential(self, tenant_id: str, repo_url: str) -> RepoCredential | None:
        return None


class OpenBaoSecretProvider:
    def __init__(
        self, settings: Settings, *, client: Any = None, http: httpx.AsyncClient | None = None
    ) -> None:
        self.client = client or hvac.Client(url=settings.openbao_url, token=settings.openbao_token)
        self.mount = settings.openbao_mount
        self.prefix = settings.openbao_prefix.strip("/")
        self.http = http
        self._cache: dict[str, RepoCredential] = {}
        self._lock = asyncio.Lock()

    async def repo_credential(self, tenant_id: str, repo_url: str) -> RepoCredential | None:
        parts = urlsplit(repo_url)
        path = parts.path.strip("/").removesuffix(".git").split("/")
        components = [tenant_id, parts.hostname or "", *path]
        if any(not re.fullmatch(r"[\w.-]+", c) or c in {".", ".."} for c in components):
            raise WorkspaceError("Invalid secret lookup path")
        base = f"{self.prefix}/{tenant_id}/repos/{parts.hostname}"
        paths = [base + "/" + "/".join(path[:i]) for i in range(len(path), 0, -1)] + [base]
        async with self._lock:
            for key in paths:
                cached = self._cache.get(key)
                if cached and cached.expires_at > datetime.now(UTC) + timedelta(minutes=5):
                    return cached
                try:
                    response = await asyncio.to_thread(
                        self.client.secrets.kv.v2.read_secret_version,
                        path=key,
                        mount_point=self.mount,
                        raise_on_deleted_version=True,
                    )
                except hvac.exceptions.InvalidPath:
                    continue
                except Exception:  # noqa: BLE001 -- provider errors must not expose secret response bodies
                    raise WorkspaceError("Secret store lookup failed") from None
                data = response["data"]["data"]
                try:
                    if data["type"] == "token":
                        return RepoCredential(username=data["username"], password=data["token"])
                    if data["type"] != "github_app":
                        raise ValueError("Unknown credential type")
                    credential = await self._installation(data)
                except Exception:  # noqa: BLE001 -- provider errors must not expose secret response bodies
                    raise WorkspaceError("Repository credential acquisition failed") from None
                self._cache[key] = credential
                return credential
        return None

    async def _installation(self, data: dict) -> RepoCredential:
        signed = github_app_jwt(data)

        async def exchange(http: httpx.AsyncClient) -> RepoCredential:
            response = await http.post(
                f"{data.get('api', 'https://api.github.com').rstrip('/')}/app/installations/{data['installation_id']}/access_tokens",
                headers={
                    "Authorization": f"Bearer {signed}",
                    "Accept": "application/vnd.github+json",
                },
            )
            response.raise_for_status()
            result = response.json()
            return RepoCredential(
                username="x-access-token",
                password=result["token"],
                expires_at=datetime.fromisoformat(result["expires_at"]),
            )

        if self.http:
            return await exchange(self.http)
        async with httpx.AsyncClient(timeout=30) as http:
            return await exchange(http)


def make_secret_provider(settings: Settings) -> SecretProvider:
    return (
        OpenBaoSecretProvider(settings)
        if settings.secret_provider == "openbao"
        else HostGitCredentials()
    )


def git_env(credential: RepoCredential | None) -> dict[str, str]:
    import os
    from pathlib import Path

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if credential:
        env.update(
            GIT_ASKPASS=str(Path(__file__).parents[1] / "workspace/askpass.sh"),
            SCA_GIT_USERNAME=credential.username,
            SCA_GIT_PASSWORD=credential.password,
        )
    return env


def redact(text: str, credential: RepoCredential | None = None) -> str:
    from urllib.parse import quote

    if credential:
        for value in (credential.password, credential.username):
            if value:
                text = text.replace(value, "<redacted>").replace(
                    quote(value, safe=""), "<redacted>"
                )
    return re.sub(r"(https?://)[^/\s@]+@", r"\1<redacted>@", text)
