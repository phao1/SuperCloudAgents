"""Model gateway backed by a LiteLLM proxy: per-turn virtual keys with a budget and a TTL.

The real provider credentials live only in the LiteLLM process. A sandbox gets a
virtual key that works for one agent turn and is deleted when the turn ends.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math

import httpx

from sca.core.model_provider import GatewayError, ModelProviderError, classify_model_error
from sca.gateway.health import ProviderHealth


class LiteLLMGateway:
    def __init__(self, admin_url: str, master_key: str, sandbox_url: str, timeout_s: float = 20,
                 *, provider_health: ProviderHealth | None = None):
        self._master_key = master_key
        self._provider_health = provider_health
        self.sandbox_url = sandbox_url  # what a sandbox uses as its OpenAI-compatible base URL
        self._http = httpx.AsyncClient(
            base_url=admin_url.rstrip("/"),
            headers={"Authorization": f"Bearer {master_key}"},
            timeout=timeout_s,
            limits=httpx.Limits(keepalive_expiry=2),
        )

    TRANSIENT_ERRORS = (
        httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
        httpx.WriteError, httpx.PoolTimeout,
    )

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(4):
            try:
                return await self._http.request(method, path, **kwargs)
            except self.TRANSIENT_ERRORS:
                if attempt == 3:
                    raise GatewayError("model gateway transport failed") from None
                await asyncio.sleep(0.1 * 2**attempt)
            except httpx.HTTPError:
                raise GatewayError("model gateway unreachable") from None
        raise AssertionError("unreachable")

    async def _post(self, path: str, body: dict) -> dict:
        r = await self._request("POST", path, json=body)
        if r.status_code == 404 and path == "/key/delete" and "no keys found" in r.text.lower():
            return {}
        if r.status_code >= 400:
            raise GatewayError(f"model gateway {path} failed: {r.status_code}")
        return r.json()

    def _observe_model(self, error: ModelProviderError | None) -> None:
        if self._provider_health is not None:
            self._provider_health.record(error)

    async def _post_model(self, path: str, body: dict) -> dict:
        try:
            response = await self._request("POST", path, json=body)
        except GatewayError:
            error = ModelProviderError("unavailable")
            self._observe_model(error)
            raise error from None
        if response.status_code >= 400:
            error = classify_model_error(response.text, status=response.status_code)
            if error is not None:
                self._observe_model(error)
                raise error
            raise GatewayError(f"model gateway {path} failed: {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise GatewayError("invalid model gateway response") from None
        if isinstance(data, dict):
            if data.get("error") is None:
                return data
            if error := classify_model_error(data):
                self._observe_model(error)
                raise error
        raise GatewayError("invalid model gateway response")

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        if not texts:
            return []
        data = await self._post_model("/v1/embeddings", {"model": model, "input": texts})
        try:
            rows = sorted(data["data"], key=lambda row: row["index"])
            if [row["index"] for row in rows] != list(range(len(texts))):
                raise ValueError("invalid indices")
            vectors = [[float(x) for x in row["embedding"]] for row in rows]
            if (not vectors or not vectors[0] or
                    any(len(v) != len(vectors[0]) or
                        not all(math.isfinite(x) for x in v) for v in vectors)):
                raise ValueError("invalid vectors")
        except (KeyError, TypeError, ValueError):
            raise GatewayError("invalid embedding response") from None
        self._observe_model(None)
        return vectors

    async def complete(self, messages: list[dict[str, str]], model: str, *,
                       max_tokens: int = 1024) -> str:
        data = await self._post_model('/v1/chat/completions', {
            'model': model, 'messages': messages, 'max_tokens': max_tokens, 'temperature': 0,
        })
        try:
            content = data['choices'][0]['message']['content']
            if not isinstance(content, str):
                raise TypeError('missing content')
        except (KeyError, IndexError, TypeError, ValueError):
            raise GatewayError('invalid completion response') from None
        self._observe_model(None)
        return content

    async def issue_key(
        self,
        alias: str,
        *,
        models: list[str],
        ttl_s: int,
        max_budget_usd: float | None = None,
        metadata: dict | None = None,
    ) -> str:
        # Supplying a stable key makes a lost generate response safe to recover.
        key = "sk-" + hmac.new(
            self._master_key.encode(), alias.encode(), hashlib.sha256
        ).hexdigest()

        async def existing() -> str | None:
            response = await self._request(
                "GET", "/key/list",
                params={"key_alias": alias, "return_full_object": "true"},
            )
            response.raise_for_status()
            for row in response.json().get("keys", []):
                if row.get("key_alias") != alias:
                    continue
                token = row.get("key") or row.get("token")
                if token == key or token == hashlib.sha256(key.encode()).hexdigest():
                    return key
                if isinstance(token, str) and token.startswith("sk-"):
                    return token
                # Older randomly generated keys are stored only as hashes by LiteLLM.
                await self._post("/key/delete", {"key_aliases": [alias]})
                return None
            return None

        found = await existing()
        if found:
            return found
        body = {
            "key": key, "key_alias": alias, "models": models,
            "duration": f"{int(ttl_s)}s", "max_budget": max_budget_usd,
            "metadata": metadata or {},
        }
        try:
            data = await self._post("/key/generate", body)
        except GatewayError:
            found = await existing()
            if found:
                return found
            raise
        return data["key"]

    # LiteLLM writes spend in batches (proxy_batch_write_at). Spend counts as settled once it
    # has not changed for longer than one batch interval.
    # Measured: a request's spend can reach the key's total more than 4 s after the request
    # ended, even with proxy_batch_write_at=1. Settled = unchanged for 5 s.
    SETTLE_POLLS = 10  # x 0.5 s
    SETTLE_TIMEOUT_S = 20

    async def _spend(self, alias: str) -> tuple[float | None, bool]:
        previous = None
        stable = 0
        spend = None
        try:
            async with asyncio.timeout(self.SETTLE_TIMEOUT_S):
                while True:
                    response = await self._request("GET", 
                        "/key/list", params={"key_alias": alias, "return_full_object": "true"}
                    )
                    response.raise_for_status()
                    keys = response.json().get("keys") or []
                    if keys:
                        token = keys[0].get("token") or keys[0].get("key")
                        if token:
                            response = await self._request("GET", "/key/info", params={"key": token})
                            response.raise_for_status()
                            info = response.json().get("info") or {}
                        else:
                            info = keys[0]
                        value = info.get("spend")
                        if value is not None:
                            spend = float(value)
                            if not math.isfinite(spend) or spend < 0:
                                return None, False
                            stable = stable + 1 if spend == previous else 0
                            if stable >= self.SETTLE_POLLS:
                                return spend, True
                            previous = spend
                    await asyncio.sleep(0.5)
        except TimeoutError:
            return spend, False
        except (GatewayError, httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError):
            return None, False

    async def finalize_key(self, alias: str) -> tuple[float | None, bool]:
        """Settle asynchronous spend before deleting the credential."""
        try:
            return await self._spend(alias)
        finally:
            await self._post("/key/delete", {"key_aliases": [alias]})

    async def revoke_key(self, alias: str) -> float | None:
        spend, _ = await self.finalize_key(alias)
        return spend

    async def list_aliases(self, prefix: str) -> list[str]:
        aliases: set[str] = set()
        page = 1
        while True:
            response = await self._request("GET", 
                "/key/list", params={"return_full_object": "true", "page": page, "size": 100}
            )
            response.raise_for_status()
            data = response.json()
            keys = data.get("keys") or []
            for key in keys:
                alias = key.get("key_alias") if isinstance(key, dict) else None
                if isinstance(alias, str) and alias.startswith(prefix):
                    aliases.add(alias)
            if not keys or page >= data.get("total_pages", 1):
                return sorted(aliases)
            page += 1

    async def aclose(self) -> None:
        await self._http.aclose()
