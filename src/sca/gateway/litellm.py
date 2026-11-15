"""Model gateway backed by a LiteLLM proxy: per-turn virtual keys with a budget and a TTL.

The real provider credentials live only in the LiteLLM process. A sandbox gets a
virtual key that works for one agent turn and is deleted when the turn ends.
"""

from __future__ import annotations

import httpx

from sca.core.errors import ScaError


class GatewayError(ScaError):
    pass


class LiteLLMGateway:
    def __init__(self, admin_url: str, master_key: str, sandbox_url: str, timeout_s: float = 20):
        self.sandbox_url = sandbox_url  # what a sandbox uses as its OpenAI-compatible base URL
        self._http = httpx.AsyncClient(
            base_url=admin_url.rstrip("/"),
            headers={"Authorization": f"Bearer {master_key}"},
            timeout=timeout_s,
        )

    async def _post(self, path: str, body: dict) -> dict:
        try:
            r = await self._http.post(path, json=body)
        except httpx.HTTPError as e:
            raise GatewayError(f"model gateway unreachable: {e}") from e
        if r.status_code >= 400:
            raise GatewayError(f"model gateway {path} failed: {r.status_code} {r.text[:300]}")
        return r.json()

    async def issue_key(
        self,
        alias: str,
        *,
        models: list[str],
        ttl_s: int,
        max_budget_usd: float | None = None,
        metadata: dict | None = None,
    ) -> str:
        # A retried activity asks again under the same alias: replace the old key.
        await self.revoke_key(alias)
        data = await self._post(
            "/key/generate",
            {
                "key_alias": alias,
                "models": models,
                "duration": f"{int(ttl_s)}s",
                "max_budget": max_budget_usd,
                "metadata": metadata or {},
            },
        )
        return data["key"]

    async def revoke_key(self, alias: str) -> float | None:
        """Delete the key and return what it spent in USD, if the gateway knows."""
        spend = None
        try:
            r = await self._http.get("/key/list", params={"key_alias": alias, "return_full_object": "true"})
            if r.status_code == 200:
                keys = r.json().get("keys") or []
                if keys and isinstance(keys[0], dict):
                    spend = keys[0].get("spend")
                if not keys:
                    return None
        except httpx.HTTPError as e:
            raise GatewayError(f"model gateway unreachable: {e}") from e
        await self._post("/key/delete", {"key_aliases": [alias]})
        return spend

    async def aclose(self) -> None:
        await self._http.aclose()
