"""Credentials component: the agent works through the model gateway with a per-turn key.

Needs `docker compose --profile gateway up -d` and DEEPSEEK_API_KEY in .env. Spends a
little on the provider account.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from sca.sandbox.docker import DockerSandboxProvider

from .conftest import git, sandbox_running

pytestmark = [pytest.mark.e2e, pytest.mark.gateway]

GATEWAY = "http://localhost:4000"


def _env_file() -> dict[str, str]:
    path = Path(__file__).parents[2] / ".env"
    if not path.exists():
        return {}
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY") or _env_file().get("LITELLM_MASTER_KEY", "")


@pytest.fixture
def agent_backend() -> str:
    return "codex"


@pytest.fixture
def settings_overrides() -> dict:
    try:
        httpx.get(f"{GATEWAY}/health/liveliness", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("model gateway is not running")
    return {
        "model_gateway_url": GATEWAY,
        "model_gateway_master_key": MASTER_KEY,
        "default_model": "deepseek-flash",
        # The model service is no longer on the sandbox's allowlist: only the gateway is.
        "egress_allow": "pypi.org,files.pythonhosted.org",
    }


async def gateway_keys(task_id: str) -> list[str]:
    async with httpx.AsyncClient(
        base_url=GATEWAY, headers={"Authorization": f"Bearer {MASTER_KEY}"}, timeout=20
    ) as http:
        r = await http.get("/key/list", params={"return_full_object": "true", "size": 100})
        r.raise_for_status()
        return [
            k.get("key_alias") or ""
            for k in r.json().get("keys", [])
            if isinstance(k, dict) and task_id in (k.get("key_alias") or "")
        ]


async def gateway_spend(task_id: str) -> float:
    async with httpx.AsyncClient(
        base_url=GATEWAY, headers={"Authorization": f"Bearer {MASTER_KEY}"}, timeout=20
    ) as http:
        for _ in range(16):
            response = await http.get("/spend/logs")
            response.raise_for_status()
            data = response.json()
            logs = data if isinstance(data, list) else data.get("data", [])
            spend = 0.0
            for log in logs:
                metadata = log.get("metadata") or {}
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                if task_id in (metadata.get("user_api_key_alias") or ""):
                    spend += float(log.get("spend") or 0)
            if spend > 0:
                return spend
            await asyncio.sleep(1)
    raise AssertionError("No LiteLLM spend logs found for the task")


async def test_agent_works_with_a_per_turn_key_and_no_mounted_credential(stack, source_repo):
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": "The test suite fails because calc.add is wrong. Fix calc.add.",
            "verify_command": "python -m pytest -q",
            "turn_timeout_s": 600,
        }
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=900)
    assert task["result"]["verification_passed"] is True
    assert "+" in await stack.patch(task_id)

    # No provider credential was mounted, and none is in the sandbox's environment.
    _, inspect, _ = await DockerSandboxProvider()._cli(
        "inspect", "--format", "{{json .Mounts}} {{json .Config.Env}}", task["result"]["sandbox_id"]
    )
    assert "auth.json" not in inspect
    provider_key = _env_file().get("DEEPSEEK_API_KEY", "missing")
    assert provider_key not in inspect and MASTER_KEY not in inspect
    agent_home = stack.settings.task_dir(task_id) / "agent_home"
    assert not (agent_home / "auth.json").exists()
    for path in agent_home.rglob("*"):
        if path.is_file() and path.stat().st_size < 2_000_000:
            data = path.read_bytes()
            assert provider_key.encode() not in data and MASTER_KEY.encode() not in data

    # The turn's key was deleted when the turn ended; nothing usable is left behind.
    assert await gateway_keys(task_id) == []

    await stack.review(task_id, "approve")
    task = await stack.wait_for(task_id, "SUCCEEDED")
    assert task["usage"].get("input_tokens", 0) > 0
    assert task["usage"].get("cost_micro_usd", 0) > 0
    assert task["usage"]["cost_micro_usd"] / 1e6 == pytest.approx(
        await gateway_spend(task_id), rel=0.05, abs=0.000001
    )
    assert not sandbox_running(task_id)
    assert git(source_repo, "show", "main:calc.py")  # the source repo is untouched (no push)
    print("\nusage:", task["usage"])


async def test_tiny_cost_budget_stops_task(stack, source_repo):
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": "Fix calc.add and run the tests. Explain the changes in detail.",
            "verify_command": "python -m pytest -q",
            "max_cost_usd": 0.00001,
            "turn_timeout_s": 600,
        }
    )
    task = await stack.wait_for(task_id, "FAILED", timeout=900)
    assert "budget" in task["state_reason"].lower()
    turns = [e for e in await stack.events(task_id) if e["type"] == "agent.turn_started"]
    assert 1 <= len(turns) <= 2
    assert await gateway_keys(task_id) == []
