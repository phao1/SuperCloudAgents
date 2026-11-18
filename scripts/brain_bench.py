"""Compare sequential fake-model turns; requires an existing Docker sandbox image."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sca.agents.opencode_brain import OpenCodeBrainBackend
from sca.config import Settings
from sca.core.models import AgentContext


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = {"id": "chatcmpl-bench", "object": "chat.completion.chunk", "created": 1,
                 "model": "test", "choices": [{"index": 0,
                 "delta": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]}
        self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())


async def benchmark(args: argparse.Namespace) -> None:
    settings = Settings()
    temporary = tempfile.TemporaryDirectory(prefix="sca-brain-bench-")
    temporary_root = Path(temporary.name)
    settings = settings.model_copy(update={"data_dir": temporary_root})
    model = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    name = "sca-brain-bench-" + uuid.uuid4().hex
    prefix = settings.opencode_brain_docker_prefix
    if args.fake_hand:
        fake = temporary_root / "fake-docker"
        fake.write_text('#!/usr/bin/python3\nimport sys\nif sys.argv[1]=="inspect": print("none")\n')
        fake.chmod(0o700)
        prefix = [str(fake)]

    def docker(*arguments: str) -> None:
        import shlex
        if args.fake_hand:
            return
        command = ([*prefix, shlex.join(["docker", *arguments])] if prefix[0] == "sg"
                   else [*prefix, *arguments])
        subprocess.run(["timeout", "30", *command], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        await asyncio.to_thread(docker, "run", "-d", "--network=none", "--name", name,
                                "--entrypoint", "sh", args.image,
                                "-c", "mkdir -p /workspace; sleep 3600")
        for pooled in (False, True):
            agent = OpenCodeBrainBackend(
                settings.opencode_src_cache, settings.opencode_brain_bun,
                settings.opencode_brain_host, "test", docker_prefix=prefix,
                gateway_url=f"http://127.0.0.1:{model.server_port}/v1",
                settings=settings.model_copy(update={"opencode_brain_pool": pooled}))
            if args.source:
                agent.materialize = lambda version: args.source.resolve()
            overheads, elapsed = [], []
            try:
                for index in range(args.turns):
                    task = uuid.uuid4().hex
                    ctx = AgentContext(task_id=task, agent_version=args.version,
                                       agent_home_host=str(settings.data_dir / "brain-bench" / task),
                                       model_api_key="fake-benchmark-key")
                    started = time.monotonic()
                    events = [event async for event in agent.run_turn(
                        None, name, ctx, "Reply done without tools.",
                        session_id=None, timeout_s=180)]
                    elapsed.append((time.monotonic() - started) * 1000)
                    summary = next(e.data for e in events if e.data.get("summary") == "brain_turn")
                    overheads.append(max(0, summary["brain_wall_time_ms"] -
                                         summary["total_model_call_ms"] - summary["total_tool_ms"]))
                    print(json.dumps({"mode": "pool" if pooled else "per-turn", "turn": index,
                                      "pool": summary["pool"], "overhead_ms": overheads[-1]}), flush=True)
                report = {"mode": "pool" if pooled else "per-turn", "turns": args.turns,
                          "overhead_p50_ms": statistics.median(overheads),
                          "overhead_p95_ms": sorted(overheads)[math.ceil(args.turns * .95) - 1],
                          "wall_p50_ms": statistics.median(elapsed)}
                if pooled:
                    report["server"] = await agent.pool.ping(next(iter(agent.pool.servers.values())))
                    warm = overheads[1:]
                    if warm:
                        report["warm_overhead_p50_ms"] = statistics.median(warm)
                        report["warm_overhead_p95_ms"] = sorted(warm)[math.ceil(len(warm) * .95) - 1]
                print(json.dumps(report), flush=True)
            finally:
                await agent.pool.close()
    finally:
        await asyncio.to_thread(docker, "rm", "-f", name)
        model.shutdown()
        model.server_close()
        thread.join(timeout=5)
        temporary.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fake-hand", action="store_true",
                        help="Use empty fake Docker responses; does not validate container isolation")
    parser.add_argument("--source", type=Path, help="Use an existing trusted source instead of a version export")
    parser.add_argument("--turns", type=int, default=6)
    parser.add_argument("--version", default=None)
    parser.add_argument("--image", default=os.environ.get("SCA_BRAIN_IMAGE", "sca-sandbox:latest"))
    options = parser.parse_args()
    if not 1 <= options.turns <= 100:
        parser.error("--turns must be between 1 and 100")
    asyncio.run(benchmark(options))
