import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.engine import make_url

from sca.agents.opencode_brain import BrainHandTurn, OpenCodeBrainBackend
from sca.api import terminal as terminal_api
from sca.api.app import create_app
from sca.api.auth import StaticTokenAuthenticator
from sca.config import Settings
from sca.core.errors import NotFoundError
from sca.core.models import AgentContext, TaskState
from sca.sandbox.handd import DAEMON, MAX_FRAME, HanddClient
from sca.storage.local import LocalObjectStore

ROOT = Path(__file__).resolve().parents[2]
BRAIN = ROOT / "deploy/opencode/brain"
HELPER = """
import os, runpy, sys
module = runpy.run_path(sys.argv[1])
class PipeConnection:
    def makefile(self, mode):
        return os.fdopen(int(sys.argv[2]), mode)
    def sendall(self, data):
        fd = int(sys.argv[3])
        while data:
            data = data[os.write(fd, data):]
sys.exit(module["run"](PipeConnection(), sys.argv[4], sys.argv[5:]))
"""
DOCKER_FIXTURE = r"""
import base64, json, sys, time
from pathlib import Path

trace = Path(sys.argv[1])
available = sys.argv[2] == "yes"
args = sys.argv[3:]
if args[:3] == ["exec", "-i", "-w"]:
    with trace.open("a") as file:
        file.write("exec\n")
    time.sleep(.04)
    sys.stdout.buffer.write(b"shell output\n" if args[-1] == "shell" else sys.stdin.buffer.read())
    sys.exit(0)
assert args[:6] == ["exec", "-i", "sandbox", "python3", "-u", "-c"]
with trace.open("a") as file:
    file.write("start\n")
if not available:
    sys.exit(1)
for line in sys.stdin:
    request = json.loads(line)
    ident = request["id"]
    def send(**fields):
        print(json.dumps({"id": ident, **fields}), flush=True)
    if request.get("ping"):
        send(pong=True, pty=True)
    elif "argv" in request:
        assert request["cwd"] == "/workspace/sub"
        assert request["env"]["LC_ALL"] == "C"
        assert "SCA_MODEL_KEY" not in request["env"]
        time.sleep(.01)
        if request["argv"] == ["fixture", "shell"]:
            assert request["op"] == "pty.open"
            send(opened=True)
            send(stdout=base64.b64encode(b"shell output\r\n").decode())
        else:
            assert request["argv"] == ["fixture", "write"]
            assert "op" not in request
            send(stdout=request["stdin"])
        send(exit=0, timed_out=False)
"""


class PipeProtocol(asyncio.streams.FlowControlMixin):
    def __init__(self):
        super().__init__()
        self.closed = asyncio.get_running_loop().create_future()

    def connection_lost(self, exc):
        super().connection_lost(exc)
        self.closed.set_result(None)

    def _get_close_waiter(self, stream):
        return self.closed


async def pipe_streams(source: int, target: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_FRAME)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader),
                                 os.fdopen(source, "rb"))
    transport, protocol = await loop.connect_write_pipe(PipeProtocol, os.fdopen(target, "wb"))
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)


async def attach(relay: BrainHandTurn) -> tuple[int, int]:
    worker_in, local_out = os.pipe()
    local_in, worker_out = os.pipe()
    reader, writer = await pipe_streams(worker_in, worker_out)
    relay.accept(reader, writer)
    return local_in, local_out


async def spawn_helper(relay: BrainHandTurn, handd: dict, *args: str):
    assert Path(handd["socket"]) == relay.path
    source, target = await attach(relay)
    try:
        return await asyncio.create_subprocess_exec(
            handd["python"], "-I", "-S", "-c", HELPER, handd["helper"],
            str(source), str(target), *args,
            pass_fds=(source, target), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    finally:
        os.close(source)
        os.close(target)


def options(*, pty: bool, workdir: str = "/workspace/sub") -> str:
    env = {"LC_ALL": "C"}
    if pty:
        env["OPENCODE_TERMINAL"] = "1"
    return json.dumps({"cwd": workdir, "env": env, "pty": pty,
                       "buffer_stdin": not pty})


@pytest.mark.parametrize("available", [True, False])
async def test_brain_tool_transport_and_latency(tmp_path, monkeypatch, record_property, available):
    docker = tmp_path / "docker_fixture.py"
    docker.write_text(DOCKER_FIXTURE)
    trace = tmp_path / "trace"
    prefix = [sys.executable, str(docker), str(trace), "yes" if available else "no"]
    settings = Settings(_env_file=None, data_dir=tmp_path, sandbox_handd=True,
                        opencode_brain_pool=True)
    agent = OpenCodeBrainBackend(
        tmp_path, Path(sys.executable), BRAIN / "launch.py", "model",
        gateway_url="http://example.invalid/v1", docker_prefix=prefix, settings=settings,
    )
    monkeypatch.setattr(agent, "materialize", lambda _: tmp_path)
    monkeypatch.setattr(agent, "cleanup_command", lambda *_: [sys.executable, "-c", "pass"])

    async def inline(function, *args):
        return function(*args)

    monkeypatch.setattr("sca.agents.opencode_brain.asyncio.to_thread", inline)
    relay = None

    class Listener:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def start(self):
        nonlocal relay
        relay = self
        relay.server = Listener()
        return True

    monkeypatch.setattr(BrainHandTurn, "start", start)
    durations: list[int] = []
    payload = b"\xff\x00fixture\n"

    async def fake_brain(version, source, request):
        probe = await spawn_helper(relay, request["handd"], "probe")
        await probe.communicate()
        assert (probe.returncode == 0) is available
        assert relay.transport == ("handd" if available else "exec")
        for tool in ("shell", "write"):
            started = time.monotonic_ns() // 1_000_000
            if probe.returncode == 0:
                command = await spawn_helper(
                    relay, request["handd"], "run", options(pty=tool == "shell"), "fixture", tool,
                )
            else:
                command = await asyncio.create_subprocess_exec(
                    *prefix, "exec", "-i", "-w", "/workspace/sub", "sandbox", "fixture", tool,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                )
            output, _ = await command.communicate(payload if tool == "write" else None)
            ended = time.monotonic_ns() // 1_000_000
            durations.append(ended - started)
            assert command.returncode == 0
            if tool == "shell":
                assert output.replace(b"\r\n", b"\n") == b"shell output\n"
            else:
                assert output == payload
            yield {"type": "tool_use", "part": {"tool": tool, "state": {
                "time": {"start": started, "end": ended}, "input": {"command": "fixture"},
                "metadata": {"exit": command.returncode}, "output": "fixture output",
            }}}
        yield {"type": "step_finish", "part": {"reason": "stop", "tokens": {}}}

    monkeypatch.setattr(agent.pool, "stream", fake_brain)
    ctx = AgentContext(task_id=str(uuid4()), agent_home_host=str(tmp_path / "agent_home"),
                       model_api_key="fixture-model-credential")
    transport = "handd" if available else "exec"
    try:
        for _ in range(2):
            events = [event async for event in agent.run_turn(
                None, "sandbox", ctx, "fixture", session_id=None, timeout_s=6,
            )]
            assert [event.type for event in events[:2]] == ["command", "file_change"]
            assert [event.data["transport"] for event in events[:2]] == [transport, transport]
            assert [event.data["duration_ms"] for event in events[:2]] == durations[-2:]
            assert events[0].data["command"] == "fixture"
            assert events[0].data["exit_code"] == 0
            assert events[0].data["output_tail"] == "fixture output"
            assert not relay.connections
        assert trace.read_text().splitlines() == (
            ["start"] if available else ["start", "exec", "exec"] * 2
        )
        record_property(f"{transport}_call_latency_ms", durations)
    finally:
        await agent.pool.close()


def test_brain_typescript_contract():
    driver = (BRAIN / "driver.ts").read_text()
    for host in ("host.ts", "server.ts"):
        source = (BRAIN / host).read_text()
        assert "handd?: HanddInput" in source
        assert source.index("requires") < source.index("runNonInteractivePrompt({")
        assert "spawnWithSuspension(local, command" in source
    assert 'env.OPENCODE_TERMINAL === "1"' in driver
    assert 'const options = { cwd: command.options.cwd ?? input.workdir, env, pty, buffer_stdin }' in driver
    assert '"run", JSON.stringify(options), command.command, ...command.args.map(unwrap)' in driver
    assert '? handdCommand(command, input, handd) : dockerCommand(command, input)' in driver


async def test_handd_disconnect_does_not_replay(tmp_path):
    ctx = AgentContext(task_id=str(uuid4()), agent_home_host=str(tmp_path))
    relay = BrainHandTurn({}, "sandbox", ctx, tmp_path, ["unused"], 5)
    client = HanddClient([sys.executable, "-u", "-c", """
import json, sys
probe = json.loads(sys.stdin.readline())
print(json.dumps({"id": probe["id"], "pong": True, "pty": True}), flush=True)
sys.stdin.readline()
"""])
    relay.client = client
    await client.start()
    try:
        proc = await spawn_helper(
            relay, {"python": sys.executable, "helper": str(BRAIN / "handd_exec.py"),
                    "socket": str(relay.path)},
            "run", options(pty=False), "fixture", "write",
        )
        async with asyncio.timeout(5):
            output, error = await proc.communicate(b"fixture")
        assert proc.returncode == 125
        assert output == b""
        assert b"command outcome unknown" in error
        assert not client.pending
    finally:
        await client.close()


class TaskStore:
    sessionmaker = SimpleNamespace(kw={"bind": SimpleNamespace(
        url=make_url("postgresql+asyncpg://localhost/sca"),
    )})

    def __init__(self, task_id):
        self.task_id = task_id
        self.state = TaskState.RUNNING

    def for_tenant(self, tenant):
        async def get_task(task_id, tenant_id):
            if (task_id, tenant, tenant_id) != (self.task_id, "one", "one"):
                raise NotFoundError("Task not found")
            return SimpleNamespace(state=self.state)
        return SimpleNamespace(get_task=get_task)


async def test_tenant_viewer_terminal_sse_and_live_input(tmp_path, monkeypatch):
    task_id = uuid4()
    store = TaskStore(task_id)
    app = create_app(
        store=store, object_store=LocalObjectStore(tmp_path),
        authenticator=StaticTokenAuthenticator({"viewer": "one:viewer", "other": "two:viewer"}),
    )
    ctx = AgentContext(task_id=str(task_id), agent_home_host=str(tmp_path / "agent_home"))
    relay = BrainHandTurn({}, "sandbox", ctx, tmp_path, ["unused"], 10)
    monkeypatch.setattr(terminal_api, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))

    async def connect(path):
        assert path == relay.path
        return await pipe_streams(*await attach(relay))

    monkeypatch.setattr(terminal_api, "connect", connect)
    url = f"/v1/tasks/{task_id}/terminal"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get(url)).status_code == 401
        assert (await http.get(url, headers={"Authorization": "Bearer other"})).status_code == 404
        assert (await http.post(url, headers={"Authorization": "Bearer viewer"})).status_code == 405

    events: asyncio.Queue[dict] = asyncio.Queue()
    disconnect = asyncio.Event()

    async def receive():
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        events.put_nowait(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "method": "GET",
        "scheme": "http", "path": url, "query_string": b"",
        "headers": [(b"authorization", b"Bearer viewer")],
    }

    async def output_contains(expected):
        output = b""
        while expected not in output:
            packet = await events.get()
            body = packet.get("body", b"")
            if body.startswith(b"event: terminal.output\n"):
                frame = json.loads(body.splitlines()[1].removeprefix(b"data: "))
                output += base64.b64decode(frame["data"])
        return output

    client = HanddClient([sys.executable, "-u", str(DAEMON)])
    await client.start()
    relay.client = client
    stream = asyncio.create_task(app(scope, receive, send))
    proc = None
    try:
        async with asyncio.timeout(8):
            assert (await events.get())["status"] == 200
            assert (await events.get())["body"] == b": connected\n\n"
            proc = await spawn_helper(
                relay, {"python": sys.executable, "helper": str(BRAIN / "handd_exec.py"),
                        "socket": str(relay.path)},
                "run", options(pty=True, workdir=str(tmp_path)),
                sys.executable, "-c", 'print("ready", flush=True); print("read", input())',
            )
            assert b"ready\r\n" in await output_contains(b"ready\r\n")
            assert proc.returncode is None
            proc.stdin.write(b"live input\n")
            await proc.stdin.drain()
            assert b"read live input\r\n" in await output_contains(b"read live input\r\n")
            assert await proc.wait() == 0
            store.state = TaskState.SUCCEEDED
            for watcher in relay.viewers:
                watcher.close()
            await stream
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        stream.cancel()
        await asyncio.gather(stream, return_exceptions=True)
        await client.close()
