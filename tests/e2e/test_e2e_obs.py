"""Live observability contract; requires compose obs, Docker, Postgres and Temporal."""

import asyncio
import os
import socket
import sys
import time
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest

from .conftest import Stack

pytestmark = [pytest.mark.e2e, pytest.mark.live]


async def wait_until(check, timeout=90):
    async with asyncio.timeout(timeout):
        while True:
            value = await check()
            if value:
                return value
            await asyncio.sleep(1)


async def test_observability(tmp_path, source_repo):
    # Separate processes give API and worker distinct OTel service identities.
    async with httpx.AsyncClient(timeout=10) as query:
        try:
            for url in ("http://localhost:9090/-/ready", "http://localhost:16686/"):
                (await query.get(url)).raise_for_status()
            docker = await asyncio.create_subprocess_exec(
                "docker",
                "info",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await docker.wait():
                pytest.skip("Docker unavailable")
        except (httpx.HTTPError, OSError):
            pytest.skip("compose obs or Docker unavailable")
        for port in (8000, 9464, 9465):
            with socket.socket() as probe:
                # Like the servers themselves: a TIME_WAIT from a previous run is not "in use".
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError:
                    pytest.skip(f"observability test requires port {port}")

        async def count():
            response = await query.get(
                "http://localhost:9090/api/v1/query",
                params={"query": "sum(sca_task_duration_seconds_count)"},
            )
            response.raise_for_status()
            result = response.json()["data"]["result"]
            return float(result[0]["value"][1]) if result else 0

        started = time.time() - 1
        queue = f"obs-{uuid4()}"
        env = {
            **os.environ,
            "SCA_OTLP_ENDPOINT": "http://localhost:4318",
            "SCA_WORKER_METRICS_PORT": "9464",
            "SCA_MAINTENANCE_SCHEDULE": "false",
            "SCA_TASK_QUEUE": queue,
            "SCA_AGENT_BACKEND": "fake",
            "SCA_DATA_DIR": str(tmp_path / "data"),
            "SCA_API_TOKENS": "obs-token:default",
        }
        python = sys.executable  # not .resolve()d: that would leave the virtual environment
        processes = []
        try:
            for args in (
                ("-m", "sca.orchestration.worker"),
                (
                    "-m",
                    "uvicorn",
                    "sca.api.app:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8000",
                ),
            ):
                log = (tmp_path / f"process-{len(processes)}.log").open("wb")
                try:
                    proc = await asyncio.create_subprocess_exec(
                        python, *args, env=env, stdout=log, stderr=log
                    )
                finally:
                    log.close()
                processes.append(proc)
            async with httpx.AsyncClient(
                base_url="http://localhost:8000",
                timeout=30,
                headers={"Authorization": "Bearer obs-token"},
            ) as http:

                async def ready():
                    try:
                        return (await http.get("/metrics")).status_code == 200
                    except httpx.HTTPError:
                        return False

                await wait_until(ready)

                async def worker_scraped():
                    response = await query.get(
                        "http://localhost:9090/api/v1/query",
                        params={"query": 'process_start_time_seconds{job="sca-worker-tasks"}'},
                    )
                    response.raise_for_status()
                    results = response.json()["data"]["result"]
                    return any(float(row["value"][1]) >= started for row in results)

                await wait_until(worker_scraped)
                before = await count()
                stack = Stack(http, None, None)
                task_id = await stack.submit(
                    {
                        "repo_url": str(source_repo),
                        "instruction": "Fix add.\n```sca-fake\nsed -i 's/a - b/a + b/' calc.py\n```",
                        "verify_command": "python -m pytest -q",
                    }
                )
                await stack.wait_for(task_id, "AWAITING_REVIEW")
                await stack.review(task_id, "approve")
                task = await stack.wait_for(task_id, "SUCCEEDED")
                trace_id = task["result"]["trace_id"]

                async def spans_ready():
                    response = await query.get(f"http://localhost:16686/api/traces/{trace_id}")
                    if response.status_code != 200:
                        return False
                    data = response.json().get("data") or []
                    spans = [s for trace in data for s in trace.get("spans", [])]
                    names = [s["operationName"] for s in spans]
                    services = {
                        p["serviceName"]
                        for trace in data
                        for p in trace.get("processes", {}).values()
                    }
                    return (
                        any("POST" in n for n in names)
                        and any("RunWorkflow" in n for n in names)
                        and any("RunActivity" in n for n in names)
                        and {"sca-api", "sca-worker"} <= services
                    )

                await wait_until(spans_ready)

                async def increased():
                    return await count() > before

                await wait_until(increased)
        finally:
            for proc in processes:
                if proc.returncode is None:
                    proc.terminate()
            for proc in processes:
                with suppress(TimeoutError):
                    async with asyncio.timeout(10):
                        await proc.wait()
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
