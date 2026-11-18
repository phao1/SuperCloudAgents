import json
from pathlib import Path

import pytest

from sca.agents.opencode_brain import OpenCodeBrainBackend
from sca.core.models import AgentContext


def backend(tmp_path: Path) -> OpenCodeBrainBackend:
    executable = tmp_path / "bun"
    executable.touch()
    agent = OpenCodeBrainBackend(tmp_path, executable, executable, "test-model",
                                 gateway_url="http://localhost/v1")
    agent.settings = agent.settings.model_copy(update={"opencode_brain_pool": False})
    agent.materialize = lambda version: tmp_path
    agent.cleanup_command = lambda container, marker: ["/bin/true"]
    return agent


def test_secret_boundary(tmp_path):
    agent = backend(tmp_path)
    ctx = AgentContext(task_id="task", agent_home_host=str(tmp_path),
                       model_api_key="test-only-credential", model_base_url="http://localhost/v1")
    assert agent.sandbox_additions(ctx).model_dump() == {"mounts": [], "env": {}}
    assert ctx.model_api_key not in json.dumps(agent.request("container", ctx, "hello", None))
    assert ctx.model_api_key not in " ".join(agent.command())
    assert agent.environment(ctx)["SCA_MODEL_KEY"] == ctx.model_api_key
    assert "OPENCODE_CONFIG_CONTENT" not in agent.environment(ctx)


@pytest.mark.asyncio
async def test_local_stream_mapping(tmp_path):
    agent = backend(tmp_path)
    fixture = tmp_path / "events.py"
    lines = [
        {"type": "step_start", "timestamp": 100, "sessionID": "ses_test", "part": {}},
        {"type": "tool_use", "part": {"tool": "shell", "state": {
            "time": {"start": 160, "end": 190},
            "input": {"command": "echo hello"}, "output": "hello"}}},
        {"type": "text", "part": {"text": "done"}},
        {"type": "step_finish", "timestamp": 200,
         "part": {"reason": "stop", "tokens": {"input": 2}}},
    ]
    fixture.write_text("import sys\nsys.stdin.read()\n" + "\n".join(
        f"print({json.dumps(json.dumps(line))})" for line in lines))
    import sys
    agent.command = lambda: [sys.executable, str(fixture)]
    ctx = AgentContext(task_id="task", agent_home_host=str(tmp_path), model_api_key="fake",
                       model_base_url="http://localhost/v1")
    events = [event async for event in agent.run_turn(
        None, "container", ctx, "hello", session_id=None, timeout_s=5)]
    assert [event.type for event in events] == [
        "session_started", "command", "message", "usage", "usage", "turn_completed"]
    assert events[-1].data["final_message"] == "done"
    assert events[-2].data["brain_wall_time_ms"] >= 0
    assert events[-2].data["total_model_call_ms"] == 60
    assert events[-2].data["total_tool_ms"] == 30
    assert events[1].data["started_at_ms"] == 160


@pytest.mark.asyncio
@pytest.mark.parametrize("script", [
    "import sys; sys.stdin.read()",
    "import sys; sys.stdin.read(); sys.exit(2)",
    'import sys; sys.stdin.read(); print(\'{"type":"error","error":{"message":"failed"}}\')',
])
async def test_failed_or_incomplete_turn(tmp_path, script):
    import sys

    from sca.core.errors import AgentTurnError

    agent = backend(tmp_path)
    agent.command = lambda: [sys.executable, "-c", script]
    ctx = AgentContext(task_id="task", agent_home_host=str(tmp_path), model_api_key="fake",
                       model_base_url="http://localhost/v1")
    with pytest.raises(AgentTurnError):
        async for _event in agent.run_turn(
            None, "container", ctx, "hello", session_id=None, timeout_s=5,
        ):
            pass


@pytest.mark.asyncio
async def test_turn_timeout(tmp_path):
    import sys

    from sca.core.errors import AgentTurnError

    agent = backend(tmp_path)
    agent.command = lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    ctx = AgentContext(task_id="task", agent_home_host=str(tmp_path), model_api_key="fake",
                       model_base_url="http://localhost/v1")
    with pytest.raises(AgentTurnError, match="timed out"):
        async for _event in agent.run_turn(
            None, "container", ctx, "hello", session_id=None, timeout_s=0.1,
        ):
            pass


def test_host_gateway_and_cleanup(tmp_path):
    from sca.config import Settings

    settings = Settings(_env_file=None, model_gateway_url="http://127.0.0.1:4000")
    assert settings.opencode_brain_gateway_url == "http://127.0.0.1:4000/v1"
    agent = backend(tmp_path)
    ctx = AgentContext(task_id="task", agent_home_host=str(tmp_path),
                       model_base_url="http://host.docker.internal:4000/v1")
    assert agent.request("c", ctx, "p", None)["gateway_url"] == "http://localhost/v1"
    command = OpenCodeBrainBackend.cleanup_command(agent, "container", "unique-marker")
    assert command[:3] == ["docker", "exec", "container"]
    assert command[-1] == "unique-marker"
    assert "os.killpg" in command[-2]
    compile(command[-2], "brain-cleanup", "exec")
    agent.docker_prefix = ["sg", "docker", "-c"]
    assert OpenCodeBrainBackend.cleanup_command(agent, "c", "m")[:3] == agent.docker_prefix


def test_recorded_timing():
    from sca.agents.opencode import parse_opencode_line

    steps = {}
    records = [
        {"type": "step_start", "timestamp": 100, "part": {"messageID": "m"}},
        {"type": "tool_use", "part": {"messageID": "m", "tool": "shell",
         "state": {"time": {"start": 160, "end": 190}}}},
        {"type": "step_finish", "timestamp": 200, "part": {"messageID": "m"}},
    ]
    events = [event for raw in records for event in parse_opencode_line(json.dumps(raw), steps)]
    assert events[0].data["duration_ms"] == 30
    assert events[1].data["model_call_duration_ms"] == 60
    assert events[1].data["started_at_ms"] == 100
    assert events[1].data["ended_at_ms"] == 200


def test_materialize_version_and_prune(tmp_path):
    import subprocess

    from sca.config import Settings

    def git(*args):
        return subprocess.check_output(["timeout", "20", "git", *args], text=True).strip()

    repo = tmp_path / "repo"
    git("init", "--quiet", str(repo))
    (repo / "packages/sdk").mkdir(parents=True)
    (repo / "packages/sdk/package.json").write_text('{"name":"@opencode/sdk"}')
    (repo / "defect").write_text("planted")
    git("-C", str(repo), "add", ".")
    git("-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--quiet", "-m", "fixture")
    mirror = tmp_path / "mirror.git"
    git("clone", "--quiet", "--bare", str(repo), str(mirror))
    commit = git("--git-dir", str(mirror), "rev-parse", "HEAD")
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", opencode_repo_url=str(mirror))
    version = settings.opencode_default_version
    directory = settings.opencode_versions_dir / version
    directory.mkdir(parents=True)
    (directory / "meta.json").write_text(json.dumps({"commit": commit, "source_repo": str(mirror)}))
    cache = tmp_path / "cache"
    (cache / "node_modules/@opencode").mkdir(parents=True)
    (cache / "node_modules/third-party").mkdir()
    executable = tmp_path / "bun"
    executable.touch()
    agent = OpenCodeBrainBackend(cache, executable, executable, "model", settings=settings)
    source = agent.materialize(None)
    assert (source / "defect").read_text() == "planted"
    assert (source / "node_modules/@opencode/sdk").resolve() == source / "packages/sdk"
    assert (source / "node_modules/third-party").is_symlink()
    assert agent.materialize(version) == source
    obsolete = source.parent / "2.3.0-sca.999"
    obsolete.mkdir()
    agent.materialize(version)
    assert not obsolete.exists()
    (source / ".sca-commit").write_text("wrong")
    from sca.core.errors import AgentTurnError
    with pytest.raises(AgentTurnError, match="marker"):
        agent.materialize(version)


@pytest.mark.parametrize("pooled", [False, True])
async def test_cancel_runs_remote_cleanup(tmp_path, pooled):
    import asyncio
    import sys
    from unittest.mock import AsyncMock

    agent = backend(tmp_path)
    agent.settings = agent.settings.model_copy(update={"opencode_brain_pool": pooled})
    ready = asyncio.Event()
    closed = asyncio.Event()
    markers = []

    async def pool_stream(version, source, request):
        markers.append(request['turn_marker'])
        try:
            ready.set()
            await asyncio.Future()
            yield {}
        finally:
            closed.set()

    agent.pool.stream = pool_stream
    agent.command = lambda: [sys.executable, "-c", "import time; time.sleep(300)"]
    original = asyncio.create_subprocess_exec
    cleanup = AsyncMock()

    async def spawn(*args, **kwargs):
        if args[0] == '/bin/true':
            await cleanup(*args)
        proc = await original(*args, **kwargs)
        if args[0] != '/bin/true':
            ready.set()
        return proc

    from unittest.mock import patch

    ctx = AgentContext(task_id='cancel', agent_home_host=str(tmp_path), model_api_key='fake')

    async def consume():
        async for _ in agent.run_turn(None, 'container', ctx, 'wait',
                                      session_id=None, timeout_s=30):
            pass

    with patch('asyncio.create_subprocess_exec', spawn):
        task = asyncio.create_task(consume())
        await asyncio.wait_for(ready.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    cleanup.assert_awaited_once()
    if pooled:
        assert closed.is_set()
        assert len(markers) == 1 and len(markers[0]) == 32


async def test_cleanup_kills_group_without_environment_marker(tmp_path):
    import asyncio
    import sys
    import uuid

    agent = backend(tmp_path)
    marker = uuid.uuid4().hex
    marker_file = Path('/tmp') / f'sca-brain-{marker}-test'
    cancelled = Path('/tmp') / f'sca-brain-{marker}.cancelled'
    child = await asyncio.create_subprocess_exec(
        sys.executable, '-c', 'import time; time.sleep(300)',
        env={}, start_new_session=True)
    marker_file.write_text(str(child.pid))
    try:
        command = OpenCodeBrainBackend.cleanup_command(agent, 'unused', marker)
        proc = await asyncio.create_subprocess_exec(sys.executable, '-c', command[-2], marker)
        assert await asyncio.wait_for(proc.wait(), 5) == 0
        assert await asyncio.wait_for(child.wait(), 5) == -9
        assert not marker_file.exists()
        assert cancelled.exists()
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        marker_file.unlink(missing_ok=True)
        cancelled.unlink(missing_ok=True)


async def test_suspend_control_mapping_and_cleanup_resume(tmp_path):
    import sys
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    agent = backend(tmp_path)
    records = [
        {"sca_suspend_transition": True, "paused": True, "at": 100.0},
        {"sca_suspend_transition": True, "paused": False, "at": 103.0},
        {"sca_suspend": True, "suspended_s": 3.0},
        {"type": "step_finish", "part": {"reason": "stop", "tokens": {}}},
    ]
    script = "import sys; sys.stdin.read()\n" + "\n".join(
        f"print({json.dumps(json.dumps(record))})" for record in records)
    agent.command = lambda: [sys.executable, "-c", script]
    sandbox = SimpleNamespace(unpause=AsyncMock())
    ctx = AgentContext(task_id="task", agent_home_host=str(tmp_path), model_api_key="fake")
    events = [event async for event in agent.run_turn(
        sandbox, "one", ctx, "hello", session_id=None, timeout_s=5)]
    assert events[0].data == {"summary": "sandbox_transition", "paused": True, "at": 100.0}
    assert events[1].data["paused"] is False
    assert events[2].data["sandbox_suspended_s"] == 3
    sandbox.unpause.assert_awaited_once_with("one")
