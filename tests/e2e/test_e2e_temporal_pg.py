"""Temporal on Postgres: the orchestrator itself can restart without losing tasks.

Needs: docker compose --profile temporal-pg up -d
"""

from __future__ import annotations

import asyncio
import socket
import subprocess

import pytest
from temporalio.client import Client

from .test_e2e_fake import ADD_SUB, RIGHT_FIX, block

pytestmark = [pytest.mark.e2e, pytest.mark.live]

ADDRESS = "localhost:7234"


async def _ready() -> bool:
    try:
        client = await Client.connect(ADDRESS)
        _ = [w async for w in client.list_workflows(page_size=1)]
        return True
    except Exception:  # noqa: BLE001 - any failure means not ready yet
        return False


@pytest.fixture
def settings_overrides() -> dict:
    try:
        socket.create_connection(("localhost", 7234), timeout=2).close()
    except OSError:
        pytest.skip("Temporal with Postgres persistence is not running")
    return {"temporal_address": ADDRESS}


async def test_task_survives_a_temporal_server_restart(stack, source_repo):
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": f"Fix add.\n{block(RIGHT_FIX)}",
            "verify_command": "python -m pytest -q",
        }
    )
    await stack.wait_for(task_id, "AWAITING_REVIEW")
    seq = (await stack.events(task_id))[-1]["seq"]

    await asyncio.to_thread(
        subprocess.run, ["docker", "restart", "sca-temporal-pg"], check=True, capture_output=True
    )
    async with asyncio.timeout(180):
        while not await _ready():
            await asyncio.sleep(2)

    # The workflow, its position and its pending wait all came back from Postgres.
    async with asyncio.timeout(120):
        while True:
            r = await stack.http.post(
                f"/v1/tasks/{task_id}/review",
                json={"decision": "request_changes", "feedback": f"Add sub.\n{block(ADD_SUB)}"},
            )
            if r.status_code == 200:
                break
            await asyncio.sleep(2)
    await stack.wait_for(task_id, "AWAITING_REVIEW", since_seq=seq, timeout=180)
    assert "def sub" in await stack.patch(task_id)
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")
