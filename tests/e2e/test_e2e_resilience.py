"""Phase 2, component 4: an idle task holds no sandbox, and a dead worker loses no task."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

import pytest

from .conftest import sandbox_running
from .test_e2e_fake import ADD_SUB, RIGHT_FIX, block

pytestmark = pytest.mark.e2e


class TestIdleRelease:
    @pytest.fixture
    def settings_overrides(self) -> dict:
        return {"review_idle_release_s": 2}

    async def test_sandbox_is_released_while_waiting_and_rebuilt_for_more_work(
        self, stack, source_repo
    ):
        task_id = await stack.submit(
            {
                "repo_url": str(source_repo),
                "instruction": f"Fix add.\n{block(RIGHT_FIX)}",
                "verify_command": "python -m pytest -q",
                # Installed outside the mounted directories, so it is lost with the sandbox.
                "setup_command": "touch /tmp/setup-ran",
            }
        )
        await stack.wait_for(task_id, "AWAITING_REVIEW")
        async with asyncio.timeout(30):
            while sandbox_running(task_id):
                await asyncio.sleep(0.5)
        events = await stack.events(task_id)
        assert any(e["type"] == "sandbox.released" for e in events)
        assert (await stack.task(task_id))["state"] == "AWAITING_REVIEW"
        assert "+    return a + b" in await stack.patch(task_id)  # still reviewable

        # More work: a new sandbox on the same workspace, with setup repeated; the verify
        # command proves both.
        await stack.review(
            task_id,
            "request_changes",
            f"Add sub.\n{block('test -f /tmp/setup-ran && ' + ADD_SUB)}",
        )
        task = await stack.wait_for(task_id, "AWAITING_REVIEW", since_seq=events[-1]["seq"])
        assert task["result"]["verification_passed"] is True
        patch = await stack.patch(task_id)
        assert "def sub" in patch and "+    return a + b" in patch
        assert any(e["type"] == "sandbox.restored" for e in await stack.events(task_id))

        await stack.review(task_id, "approve")
        await stack.wait_for(task_id, "SUCCEEDED")
        assert not sandbox_running(task_id)


class TestWorkerCrash:
    @pytest.fixture
    def in_process_worker(self) -> bool:
        return False

    @pytest.fixture
    def settings_overrides(self) -> dict:
        return {"turn_heartbeat_timeout_s": 8}

    def _spawn(self, stack) -> subprocess.Popen:
        s = stack.settings
        env = {
            **os.environ,
            "SCA_DATA_DIR": str(s.data_dir),
            "SCA_AGENT_BACKEND": "fake",
            "SCA_TASK_QUEUE": s.task_queue,
            "SCA_TURN_HEARTBEAT_TIMEOUT_S": str(s.turn_heartbeat_timeout_s),
        }
        return subprocess.Popen([sys.executable, "-m", "sca.orchestration.worker"], env=env)

    async def test_task_survives_a_worker_killed_mid_turn(self, stack, source_repo):
        first = self._spawn(stack)
        second = None
        try:
            # The marker makes the script slow only the first time, so the test can tell that
            # the turn really ran twice and that the second run saw the first one's files.
            script = (
                "if [ ! -f /agent_home/started ]; then touch /agent_home/started; sleep 300; fi; "
                + RIGHT_FIX
            )
            task_id = await stack.submit(
                {
                    "repo_url": str(source_repo),
                    "instruction": f"Fix add.\n{block(script)}",
                    "verify_command": "python -m pytest -q",
                }
            )
            await stack.wait_for(task_id, "RUNNING")
            marker = stack.settings.task_dir(task_id)
            async with asyncio.timeout(60):
                while not list(marker.rglob("started")):
                    await asyncio.sleep(0.3)
            first.send_signal(signal.SIGKILL)
            first.wait()
            assert (await stack.task(task_id))["state"] == "RUNNING"

            second = self._spawn(stack)
            task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=180)
            assert task["result"]["verification_passed"] is True
            assert "+    return a + b" in await stack.patch(task_id)
            events = await stack.events(task_id)
            assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
            started = [e for e in events if e["type"] == "agent.turn_started"]
            assert len(started) >= 1

            await stack.review(task_id, "approve")
            await stack.wait_for(task_id, "SUCCEEDED")
            assert not sandbox_running(task_id)
        finally:
            for proc in (first, second):
                if proc and proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=30)
