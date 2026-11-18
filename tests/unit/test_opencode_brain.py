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
    assert "SCA_BRAIN_TURN=$1" in command[-3]
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
