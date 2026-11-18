"""Opt in with SCA_TEST_BRAIN_DOCKER=1 on a Docker-capable host."""
import asyncio
import json
import os
import shutil
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sca.agents.opencode_brain import OpenCodeBrainBackend
from sca.config import Settings
from sca.core.models import AgentContext

pytestmark = [pytest.mark.docker, pytest.mark.skipif(
    os.environ.get("SCA_TEST_BRAIN_DOCKER") != "1", reason="Docker brain test is opt-in")]


@pytest.mark.asyncio
async def test_networkless_hands_share_pool(tmp_path):
    root = Path(__file__).resolve().parents[2]
    if not shutil.which("docker"):
        pytest.skip("docker CLI unavailable")
    source = root / ".data/opencode/src"
    bun = root / ".data/tools/bun-linux-x64/bun"
    if not source.is_dir() or not bun.is_file():
        pytest.skip("installed OpenCode workspace and Bun required")

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            token = self.headers["Authorization"].removeprefix("Bearer fake-test-key-")
            assert token in {"0", "1", "2"}
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            called = any(m.get("role") == "tool" for m in request["messages"])
            message = {"role": "assistant", "content": "done" if called else None}
            if not called:
                names = [t.get("function", {}).get("name") for t in request.get("tools", [])]
                name = "shell" if "shell" in names else "execute"
                arguments = {"command": f"printf brain-{token} > /workspace/brain-proof.txt",
                             "description": "Write proof inside sandbox"}
                if name == "execute":
                    arguments = {"code": "return await tools.shell(" + json.dumps(arguments) + ")"}
                message["tool_calls"] = [{"index": 0, "id": "call_write", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments)}}]
            choice = {"index": 0, "delta": message,
                      "finish_reason": "stop" if called else "tool_calls"}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1,
                     "model": "test", "choices": [choice]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    names = ["sca-brain-test-" + uuid.uuid4().hex for _ in range(3)]
    agent = None

    def docker(*args):
        return subprocess.run(["timeout", "30", "docker", *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    try:
        for name in names:
            docker("run", "-d", "--network=none", "--name", name, "--entrypoint", "sh",
                   os.environ.get("SCA_BRAIN_IMAGE", "sca-sandbox:latest"),
                   "-c", "mkdir -p /workspace; sleep 600")
        settings = Settings(_env_file=None, data_dir=tmp_path, opencode_brain_pool=True,
                            opencode_brain_pool_concurrency=3)
        agent = OpenCodeBrainBackend(source, bun, root / "deploy/opencode/brain/launch.py", "test",
                                     settings=settings,
                                     gateway_url=f"http://127.0.0.1:{server.server_port}/v1")
        agent.materialize = lambda version: source

        async def run(index, name):
            ctx = AgentContext(task_id=uuid.uuid4().hex,
                               agent_home_host=str(tmp_path / str(index)),
                               model_api_key=f"fake-test-key-{index}")
            events = [event async for event in agent.run_turn(
                None, name, ctx, "Write the proof file with shell, then finish.",
                session_id=None, timeout_s=180)]
            assert events[-1].type == "turn_completed"
            status = await agent.pool.ping(next(iter(agent.pool.servers.values())))
            return events[-2].data["pool"], status["pid"]

        results = await asyncio.gather(*(run(i, name) for i, name in enumerate(names)))
        assert [state for state, _ in results].count("cold") == 1
        assert len({pid for _, pid in results}) == 1
        for index, name in enumerate(names):
            assert docker("exec", name, "cat", "/workspace/brain-proof.txt") == f"brain-{index}"
            assert "SCA_MODEL_KEY" not in docker("exec", name, "env")
            assert docker("inspect", "--format", "{{.HostConfig.NetworkMode}}", name) == "none"
    finally:
        if agent is not None:
            await agent.pool.close()
        await asyncio.to_thread(subprocess.run, ["timeout", "30", "docker", "rm", "-f", *names],
                                capture_output=True, check=False)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
