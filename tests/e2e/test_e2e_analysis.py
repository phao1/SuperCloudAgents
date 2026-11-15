"""Analysis acceptance cases; run against migrated Postgres, Temporal and Docker."""

import asyncio
import json
import shutil
import subprocess

import pytest

from .conftest import sandbox_running
from .test_e2e_fake import block
from .test_e2e_resilience import TestWorkerCrash as WorkerCrashHelpers
from .test_e2e_verification import wait_event

pytestmark = pytest.mark.e2e

SCRIPT = """from pathlib import Path
import csv, json
values = [float(row['value']) for row in csv.DictReader(Path('data/sample.csv').open())]
mean = sum(values) / len(values)
out = Path('output'); out.mkdir(exist_ok=True)
(out/'report.md').write_text(f'Mean: {mean}\\n')
(out/'results.json').write_text(json.dumps({'mean': mean}, sort_keys=True))
(out/'figures').mkdir(exist_ok=True)
(out/'figures/mean.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
"""
WRITE = "cat > analysis.py <<'PY'\n" + SCRIPT + "PY\npython analysis.py"


@pytest.fixture(autouse=True)
def docker_available():
    if not shutil.which("docker"):
        pytest.skip("Docker unavailable")
    check = subprocess.run(["docker", "info"], check=False, capture_output=True, timeout=10)
    if check.returncode:
        pytest.skip("Docker daemon unreachable")
    image = subprocess.run(
        ["docker", "image", "inspect", "sca-sandbox-analysis:latest"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    if image.returncode:
        pytest.skip("Build sca-sandbox-analysis:latest first")


async def submit_analysis(stack, question, **overrides):
    response = await stack.http.post("/v1/files", files={"file": ("sample.csv", b"value\n1\n3\n")})
    assert response.status_code == 201, response.text
    response = await stack.http.post(
        "/v1/tasks",
        json={
            "kind": "analysis",
            "spec": {"question": question, "inputs": [response.json()["id"]], **overrides},
        },
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    stack.submitted.append(task_id)
    return task_id


@pytest.mark.parametrize("repair", [False, True])
async def test_analysis_report_and_reproducibility_repair(stack, repair):
    wrong = WRITE + "\necho '{\"mean\":99}' > output/results.json"
    task_id = await submit_analysis(
        stack,
        "Compute mean.\n"
        + (block(wrong) + "\n" + block("python analysis.py") if repair else block(WRITE)),
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW")
    assert task["result"]["verification_passed"]
    events = await stack.events(task_id)
    checks = [e["payload"] for e in events if e["type"] == "verification.completed"]
    assert [e["passed"] for e in checks] == ([False, True] if repair else [True])
    if repair:
        assert "Reproducibility mismatch" in checks[0]["output_tail"]
    for name in ["report.md", "results.json", "analysis.py", "figures/mean.svg"]:
        response = await stack.http.get(f"/v1/tasks/{task_id}/artifacts/{name}")
        assert response.status_code == 200 and response.content
    assert (await stack.http.get(f"/v1/tasks/{task_id}/artifacts/report.md")).text == "Mean: 2.0\n"
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")


async def test_analysis_budget_stop(stack):
    task_id = await submit_analysis(stack, block("true"), max_tokens=3, max_fix_attempts=5)
    task = await stack.wait_for(task_id, "FAILED")
    assert "budget exceeded" in task["state_reason"]
    assert len([e for e in await stack.events(task_id) if e["type"] == "agent.turn_started"]) == 2


class TestAnalysisLifecycle:
    @pytest.fixture
    def settings_overrides(self):
        return {"review_idle_release_s": 2}

    async def test_idle_release_restore_and_pause_resume(self, stack):
        task_id = await submit_analysis(stack, block(WRITE), setup_command="touch /tmp/setup-ran")
        await stack.wait_for(task_id, "AWAITING_REVIEW")
        released = await wait_event(stack, task_id, "sandbox.released")
        assert not sandbox_running(task_id)
        response = await stack.http.post(f"/v1/tasks/{task_id}/pause")
        assert response.status_code == 200
        await stack.review(
            task_id, "request_changes", block("test -f /tmp/setup-ran && python analysis.py")
        )
        await stack.wait_for(task_id, "PAUSED")
        response = await stack.http.post(f"/v1/tasks/{task_id}/resume")
        assert response.status_code == 200
        task = await stack.wait_for(task_id, "AWAITING_REVIEW", since_seq=released["seq"])
        assert task["result"]["verification_passed"]
        assert any(e["type"] == "sandbox.restored" for e in await stack.events(task_id))
        await stack.review(task_id, "approve")
        await stack.wait_for(task_id, "SUCCEEDED")


class TestAnalysisWorkerCrash:
    _spawn = WorkerCrashHelpers._spawn

    @pytest.fixture
    def in_process_worker(self):
        return False

    @pytest.fixture
    def settings_overrides(self):
        return {"turn_heartbeat_timeout_s": 8}

    async def test_worker_restart(self, stack):
        first, second = self._spawn(stack), None
        try:
            task_id = await submit_analysis(stack, block("sleep 15\n" + WRITE))
            await wait_event(stack, task_id, "agent.turn_started")
            await asyncio.sleep(2)
            first.kill()
            first.wait()
            second = self._spawn(stack)
            task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=180)
            assert task["result"]["verification_passed"]
            await stack.review(task_id, "approve")
            await stack.wait_for(task_id, "SUCCEEDED")
        finally:
            for proc in (first, second):
                if proc and proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=30)


class TestRealAnalysis:
    @pytest.fixture
    def agent_backend(self):
        return "codex"

    @pytest.mark.codex
    async def test_real_model_csv_mean(self, stack):
        csv = "value\n" + "".join(f"{i}\n" for i in range(1, 301))
        response = await stack.http.post("/v1/files", files={"file": ("sample.csv", csv.encode())})
        assert response.status_code == 201
        response = await stack.http.post(
            "/v1/tasks",
            json={
                "kind": "analysis",
                "spec": {
                    "question": "Compute the arithmetic mean of value. Report it in report.md and "
                    "results.json under key mean. Include a figure.",
                    "inputs": [response.json()["id"]],
                    "turn_timeout_s": 600,
                },
            },
        )
        assert response.status_code == 201, response.text
        task_id = response.json()["id"]
        stack.submitted.append(task_id)
        task = await stack.wait_for(task_id, "AWAITING_REVIEW", timeout=900)
        assert task["result"]["verification_passed"]
        result = json.loads(
            (await stack.http.get(f"/v1/tasks/{task_id}/artifacts/results.json")).text
        )
        assert result["mean"] == 150.5
        assert "150.5" in (await stack.http.get(f"/v1/tasks/{task_id}/artifacts/report.md")).text
        await stack.review(task_id, "approve")
        await stack.wait_for(task_id, "SUCCEEDED")
