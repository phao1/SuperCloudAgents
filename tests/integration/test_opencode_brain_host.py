"""Real EmbeddedHost with fake model and fake hand; never invokes Docker."""
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sca.agents.opencode_brain import OpenCodeBrainBackend
from sca.config import Settings
from sca.core.errors import AgentTurnError
from sca.core.models import AgentContext

pytestmark = pytest.mark.skipif(os.environ.get("SCA_TEST_BRAIN_HOST") != "1",
                                reason="real Bun/OpenCode host probe is opt-in")


@pytest.mark.asyncio
async def test_parallel_credentials_and_durable_resume(tmp_path):
    root = Path(__file__).resolve().parents[2]
    source, bun = root / ".data/opencode/src", root / ".data/tools/bun-linux-x64/bun"
    if not source.is_dir() or not bun.is_file():
        pytest.skip("installed OpenCode and Bun required")
    credentials = {f"fake-host-credential-{i}" for i in range(4)}
    observed = set()

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            key = self.headers["Authorization"].removeprefix("Bearer ")
            assert key in credentials
            observed.add(key)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {"id": "chatcmpl-host", "object": "chat.completion.chunk", "created": 1,
                     "model": "test", "choices": [{"index": 0, "delta": {
                         "role": "assistant", "content": "result-" + key[-1]}, "finish_reason": "stop"}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())

    fake = tmp_path / "fake-docker"
    fake.write_text('#!/usr/bin/python3\nimport sys\nif sys.argv[1]=="inspect": print("none")\n')
    fake.chmod(0o700)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(_env_file=None, data_dir=tmp_path, opencode_brain_pool=True)

    def backend():
        agent = OpenCodeBrainBackend(source, bun, root / "deploy/opencode/brain/launch.py", "test",
                                     settings=settings, docker_prefix=[str(fake)],
                                     gateway_url=f"http://127.0.0.1:{server.server_port}/v1")
        agent.materialize = lambda version: source
        return agent

    agent = backend()

    async def run(index, session=None, task=None):
        ctx = AgentContext(task_id=task or f"task{index}", agent_home_host=str(tmp_path / str(index)),
                           model_api_key=f"fake-host-credential-{index}")
        events = [event async for event in agent.run_turn(
            None, "fake-container", ctx, "Reply without tools.", session_id=session, timeout_s=90)]
        assert events[-1].data["final_message"] == f"result-{index}"
        return events

    try:
        results = await asyncio.gather(*(run(i) for i in range(3)))
        assert [events[-2].data["pool"] for events in results].count("cold") == 1
        status = await agent.pool.ping(next(iter(agent.pool.servers.values())))
        duplicate = backend()
        try:
            with pytest.raises(AgentTurnError, match="failed to start"):
                async for _ in duplicate.pool.stream(settings.opencode_default_version, source, {}):
                    pass
        finally:
            await duplicate.pool.close()
        assert (await agent.pool.ping(next(iter(agent.pool.servers.values()))))["pid"] == status["pid"]
        session = results[0][0].data["session_id"]
        sessions = {events[0].data["session_id"] for events in results}
        assert len(sessions) == 3
        await run(3, session, "task0")
        assert (await agent.pool.ping(next(iter(agent.pool.servers.values()))))["pid"] == status["pid"]
        await agent.pool.close()
        agent = backend()
        await run(3, session, "task0")
        assert observed == credentials
        for file in tmp_path.rglob("*"):
            if file.is_file():
                data = file.read_bytes()
                assert all(key.encode() not in data for key in credentials)
        print(f"EmbeddedHost RSS after parallel turns: {status['rss']} bytes")
    finally:
        await agent.pool.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
