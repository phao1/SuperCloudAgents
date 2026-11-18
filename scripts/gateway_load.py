"""One asyncio process: fake upstream, limiter, and weighted concurrent sessions."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import socket
import tempfile
import time
from collections import Counter
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import uvicorn
from fake_model import create_app as fake_app

from sca.config import Settings
from sca.gateway.limiter import create_app


def listener() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1024)
    sock.setblocking(False)
    return sock


def percentile(values: list[float], q: float) -> float:
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values) * q) - 1)], 4) if values else 0


async def run(args: argparse.Namespace) -> dict:
    weights = [int(w) for w in args.weights.split(",")]
    if len(weights) != 3 or min(weights) <= 0 or args.sessions < 3 or args.calls < 1:
        raise ValueError("use three positive weights, >=3 sessions, >=1 calls")
    counts = [max(1, int(args.sessions * w / sum(weights))) for w in weights]
    counts[-1] += args.sessions - sum(counts)
    tenants = [f"tenant-{i}" for i in range(3)]
    assignments = [t for n in range(max(counts))
                   for t, count in zip(tenants, counts) if n < count]
    keys = {f"local-key-{i}": t for i, t in enumerate(assignments)}
    servers, tasks, sockets = [], [], []
    with tempfile.TemporaryDirectory(prefix="sca-gateway-load-") as directory:
        async with AsyncExitStack() as stack:
            async def serve(app) -> str:
                sock = listener()
                sockets.append(sock)
                port = sock.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error"))
                servers.append(server)
                tasks.append(asyncio.create_task(server.serve(sockets=[sock])))
                while not server.started:
                    if tasks[-1].done():
                        await tasks[-1]
                    await asyncio.sleep(.01)
                return f"http://127.0.0.1:{port}"

            try:
                upstream = args.upstream or await serve(
                    fake_app(args.latency, args.fake_rpm, args.reject_every))
                settings = Settings(_env_file=None, model_gateway_url=upstream,
                                    gateway_test_keys=keys if not args.upstream else {},
                                    gateway_rpm=args.rpm, gateway_tenant_rpm=args.rpm,
                                    gateway_key_rpm=args.rpm, gateway_tpm=10000000,
                                    gateway_tenant_tpm=10000000, gateway_key_tpm=1000000,
                                    gateway_queue_timeout_s=120,
                                    gateway_pressure_file=Path(directory) / "pressure.json")
                app = create_app(settings)
                base = await serve(app)
                client = await stack.enter_async_context(httpx.AsyncClient(
                    timeout=180, limits=httpx.Limits(max_connections=args.sessions + 10),
                    trust_env=False))
                failures: Counter[str] = Counter()
                successes: Counter[str] = Counter()
                waits: dict[str, list[float]] = {t: [] for t in tenants}
                completed: list[str] = []
                start = time.monotonic()

                async def session(index: int, tenant: str):
                    key = f"local-key-{index}"
                    if args.upstream:
                        # External mode keys are provided by a private JSON file, never printed.
                        key = external_keys[tenant][index % len(external_keys[tenant])]
                    for turn in range(args.calls):
                        try:
                            response = await client.post(base + "/v1/chat/completions",
                                headers={"Authorization": f"Bearer {key}"},
                                json={"model": args.model, "max_tokens": 16,
                                      "stream": bool((index + turn) % 2),
                                      "messages": [{"role": "user", "content": "你好"}]})
                            waits[tenant].append(float(response.headers.get(
                                "x-sca-queue-wait-seconds", "0")))
                            if response.status_code != 200:
                                failures[f"http_{response.status_code}"] += 1
                            elif ("text/event-stream" in response.headers.get("content-type", "")
                                  and "[DONE]" not in response.text):
                                failures["incomplete_stream"] += 1
                            else:
                                successes[tenant] += 1
                                completed.append(tenant)
                        except httpx.HTTPError as exc:
                            failures[type(exc).__name__] += 1

                external_keys = json.loads(Path(args.keys_file).read_text()) if args.upstream else {}
                await asyncio.gather(*(session(i, t) for i, t in enumerate(assignments)))
                elapsed = time.monotonic() - start
                sample = completed[:min(counts) * args.calls]
                total = sum(successes.values())
                return {"sessions": args.sessions, "calls_per_session": args.calls,
                        "mode": "litellm" if args.upstream else "direct_fake",
                        "elapsed_s": round(elapsed, 3), "failures": dict(failures),
                        "successful_calls": total, "session_counts": dict(zip(tenants, counts)),
                        "throughput_share": {t: round(successes[t] / max(1, total), 4)
                                             for t in tenants},
                        "initial_contention_sample_calls": len(sample),
                        "initial_contention_share": {t: round(sample.count(t) / max(1, len(sample)), 4)
                                                     for t in tenants},
                        "queue_wait_seconds": {t: {"p50": percentile(waits[t], .5),
                                                   "p99": percentile(waits[t], .99)} for t in tenants},
                        "upstream_429": app.state.limiter.upstream_429._value.get()}
            finally:
                for server in servers:
                    server.should_exit = True
                await asyncio.gather(*tasks, return_exceptions=True)
                for sock in sockets:
                    sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=300)
    parser.add_argument("--calls", type=int, default=2)
    parser.add_argument("--weights", default="1,2,3")
    parser.add_argument("--rpm", type=float, default=1500)
    parser.add_argument("--fake-rpm", type=int, default=1800)
    parser.add_argument("--reject-every", type=int, default=97)
    parser.add_argument("--latency", type=float, default=.01)
    parser.add_argument("--upstream", help="Existing LiteLLM URL (omit /v1)")
    parser.add_argument("--keys-file", help="Private JSON: tenant-0/1/2 -> virtual key arrays")
    parser.add_argument("--model", default="fake")
    print(json.dumps(asyncio.run(run(parser.parse_args())), ensure_ascii=False, indent=2))
