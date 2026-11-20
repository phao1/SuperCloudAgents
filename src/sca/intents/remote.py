"""Worker-side HTTP and tenant credentials for intent providers."""

import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from pydantic import Field

from sca.config import IntentCredential, Settings
from sca.core.interfaces import SecretProvider
from sca.intents.base import MAX_SOURCE_BYTES

SecretName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")]
_JSON_LIMIT = 8 * 1024 * 1024
_asset_request: ContextVar[bool] = ContextVar("intent_asset_request", default=False)


class _HideAssetURLs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _asset_request.get()


# HTTPX logs full URLs at INFO, including signed attachment queries.
logging.getLogger("httpx").addFilter(_HideAssetURLs())


class RemoteError(ValueError):
    def __init__(self, provider: str, message: str, status: int | None = None):
        super().__init__(f"{provider}: {message}")
        self.status = status


def https_url(value: str) -> httpx.URL | None:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        return None
    if url.scheme == "https" and url.host and not url.userinfo and url.port in {None, 443}:
        return url
    return None


@dataclass
class RemoteClient:
    http: httpx.AsyncClient
    settings: Settings
    tenant_id: str
    secrets: SecretProvider | None = None

    def credential(
        self, provider: str, name: str, *, required: bool = True
    ) -> IntentCredential | None:
        credential = self.settings.intent_credentials.get(self.tenant_id, {}).get(
            f"{provider}/{name}"
        )
        if required and not credential:
            raise RemoteError(provider, "credential is not configured for this tenant")
        return credential

    async def json(self, provider: str, method: str, url: str, **kwargs: Any) -> Any:
        try:
            async with self.http.stream(method, url, follow_redirects=False, **kwargs) as response:
                self._check_status(provider, response)
                if response.status_code == 204:
                    return None
                return json.loads(await self._body(provider, response, _JSON_LIMIT))
        except (httpx.RequestError, httpx.InvalidURL):
            raise RemoteError(provider, "API request failed") from None
        except RemoteError:
            raise
        except (ValueError, RecursionError):
            raise RemoteError(provider, "invalid JSON response") from None

    async def asset(
        self,
        provider: str,
        uri: str,
        allowed: Callable[[httpx.URL], bool],
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> bytes:
        scope = _asset_request.set(True)
        try:
            url = https_url(uri)
            for _ in range(4):
                if url is None or not allowed(url):
                    raise RemoteError(provider, "unsupported attachment URL")
                async with self.http.stream(
                    "GET", url, headers=headers, follow_redirects=False, **kwargs
                ) as response:
                    location = response.headers.get("location")
                    if response.is_redirect and location:
                        url = https_url(str(url.join(location)))
                        continue
                    self._check_status(provider, response)
                    return await self._body(provider, response, MAX_SOURCE_BYTES)
            raise RemoteError(provider, "too many attachment redirects")
        except (httpx.RequestError, httpx.InvalidURL):
            raise RemoteError(provider, "attachment request failed") from None
        finally:
            _asset_request.reset(scope)

    @staticmethod
    def _check_status(provider: str, response: httpx.Response) -> None:
        if not response.is_success:
            raise RemoteError(provider, f"HTTP {response.status_code}", response.status_code)

    @staticmethod
    async def _body(provider: str, response: httpx.Response, limit: int) -> bytes:
        data = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            if len(data) + len(chunk) > limit:
                raise RemoteError(provider, "response exceeds size limit")
            data.extend(chunk)
        return bytes(data)


@asynccontextmanager
async def remote_connection(settings: Settings, tenant_id: str, secrets: SecretProvider):
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        yield RemoteClient(client, settings, tenant_id, secrets)
