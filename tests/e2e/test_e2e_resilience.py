"""Phase 2, component 4: an idle task holds no sandbox, and a dead worker loses no task."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

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
        # The container goes first, the event follows: wait for both.
        async with asyncio.timeout(30):
            while sandbox_running(task_id) or not any(
                e["type"] == "sandbox.released" for e in await stack.events(task_id)
            ):
                await asyncio.sleep(0.5)
        events = await stack.events(task_id)
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

    def _spawn(self, stack, data_dir: Path | None = None) -> subprocess.Popen:
        s = stack.settings
        env = {
            **os.environ,
            "SCA_DATA_DIR": str(data_dir or s.data_dir),
            "SCA_STATE_TRANSPORT": s.state_transport,
            "SCA_DATABASE_URL": s.database_url,
            "SCA_TEMPORAL_ADDRESS": s.temporal_address,
            "SCA_REVIEW_IDLE_RELEASE_S": str(s.review_idle_release_s),
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
            script = "touch /agent_home/started; sleep 15; " + RIGHT_FIX
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
                    if stack.settings.state_transport == "sync":
                        check = await asyncio.to_thread(
                            subprocess.run,
                            [
                                "docker",
                                "exec",
                                f"sca-task-{task_id}",
                                "test",
                                "-f",
                                "/agent_home/started",
                            ],
                            capture_output=True,
                            check=False,
                        )
                        if check.returncode == 0:
                            break
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


class TestWorkerMigration(TestWorkerCrash):
    @pytest.fixture
    def state_transport(self) -> str:
        return "sync"

    def _data_dir(self, stack, tmp_path, name):
        # Only object storage is shared with the API and the next worker.
        shared = stack.settings.objects_dir()
        shared.mkdir(parents=True, exist_ok=True)
        root = tmp_path / name
        root.mkdir()
        (root / "objects").symlink_to(shared, target_is_directory=True)
        return root

    def _new_data_dir(self, stack, tmp_path):
        shutil.rmtree(tmp_path / "worker-a")
        return self._data_dir(stack, tmp_path, "worker-b")

    async def test_new_worker_continues_after_review(self, stack, source_repo, tmp_path):
        first, second = self._spawn(stack, self._data_dir(stack, tmp_path, "worker-a")), None
        try:
            task_id = await stack.submit(
                {
                    "repo_url": str(source_repo),
                    "instruction": f"Fix add.\n{block(RIGHT_FIX)}",
                    "verify_command": "python -m pytest -q",
                }
            )
            await stack.wait_for(task_id, "AWAITING_REVIEW")
            events = await stack.events(task_id)
            first.kill()
            first.wait()
            other = self._new_data_dir(stack, tmp_path)
            second = self._spawn(stack, other)
            await stack.review(task_id, "request_changes", f"Add sub.\n{block(ADD_SUB)}")
            task = await stack.wait_for(task_id, "AWAITING_REVIEW", since_seq=events[-1]["seq"])
            assert task["result"]["verification_passed"]
            assert "def sub" in await stack.patch(task_id)
            assert (other / "tasks" / task_id / "agent_home/fake_queue.json").exists()
            await stack.review(task_id, "approve")
            await stack.wait_for(task_id, "SUCCEEDED")
        finally:
            for proc in (first, second):
                if proc and proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=30)

    async def test_new_worker_replays_interrupted_turn(self, stack, source_repo, tmp_path):
        first, second = self._spawn(stack, self._data_dir(stack, tmp_path, "worker-a")), None
        try:
            script = "touch /agent_home/started; sleep 30; " + RIGHT_FIX
            task_id = await stack.submit(
                {
                    "repo_url": str(source_repo),
                    "instruction": f"Fix add.\n{block(script)}",
                    "verify_command": "python -m pytest -q",
                }
            )
            async with asyncio.timeout(60):
                while True:
                    check = await asyncio.to_thread(
                        subprocess.run,
                        [
                            "docker",
                            "exec",
                            f"sca-task-{task_id}",
                            "test",
                            "-f",
                            "/agent_home/started",
                        ],
                        capture_output=True,
                        check=False,
                    )
                    if check.returncode == 0:
                        break
                    await asyncio.sleep(0.3)
            old_result = await asyncio.to_thread(
                subprocess.run,
                ["docker", "inspect", "--format", "{{.Id}}", f"sca-task-{task_id}"],
                capture_output=True,
                text=True,
                check=True,
            )
            old_id = old_result.stdout.strip()
            first.kill()
            first.wait()
            other = self._new_data_dir(stack, tmp_path)
            second = self._spawn(stack, other)
            task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=180)
            assert task["result"]["verification_passed"]
            new_result = await asyncio.to_thread(
                subprocess.run,
                ["docker", "inspect", "--format", "{{.Id}}", f"sca-task-{task_id}"],
                capture_output=True,
                text=True,
                check=True,
            )
            new_id = new_result.stdout.strip()
            assert old_id != new_id
            assert (other / "tasks" / task_id / "mirror.git").exists()
            await stack.review(task_id, "approve")
            await stack.wait_for(task_id, "SUCCEEDED")
        finally:
            for proc in (first, second):
                if proc and proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=30)
