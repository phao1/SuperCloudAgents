import hashlib
import json
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from temporalio.exceptions import ApplicationError

from sca.agents.opencode import OpenCodeBackend
from sca.config import Settings
from sca.core.models import AgentContext, AgentOverlay, ExecExit, ExecOutput
from sca.kernel.activities import KernelActivities
from sca.kinds.coding.spec import CodingSpec


@pytest.mark.parametrize("overlay", [
    {"unknown": "x"}, {"system_prompt": "x" * 40_001},
    {"tool_guidance": "x" * 10_001}, {"tool_descriptions": {"read": "x" * 4_001}},
    {"tool_descriptions": {f"tool{i}": "x" for i in range(65)}},
    {"tool_descriptions": {"Read": "x"}}, {"tool_descriptions": {"a" * 42: "x"}},
    {"tool_descriptions": {"read\n": "x"}}, {"system_prompt": 1},
])
def test_validation(overlay):
    with pytest.raises(ValidationError):
        CodingSpec(repo_url="r", instruction="i", agent_overlay=overlay)


def test_limits_and_serialization():
    overlay = AgentOverlay(system_prompt="x" * 40_000, tool_guidance="x" * 10_000,
                           tool_descriptions={f"tool{i}": "x" * 4_000 for i in range(64)})
    spec = CodingSpec(repo_url="r", instruction="i", agent_overlay=overlay)
    assert CodingSpec.model_validate(spec.model_dump()).agent_overlay == overlay
    assert CodingSpec(repo_url="r", instruction="i").agent_overlay is None
    assert AgentOverlay().canonical_json() == "{}"
    assert AgentOverlay(tool_descriptions={"a" * 41: ""})


@pytest.mark.parametrize("overlay", [None, AgentOverlay(), AgentOverlay(
    system_prompt="Chinese: 你好", tool_guidance="", tool_descriptions={"read": "custom"})])
async def test_backend_file_and_env(tmp_path, overlay):
    version = tmp_path / "2.3.0-sca.1"
    version.mkdir()
    (version / "opencode").touch()
    backend = OpenCodeBackend(tmp_path, version.name, "model")
    ctx = AgentContext(task_id="id", agent_home_host=str(tmp_path / "home"),
                       agent_overlay=overlay, model_base_url="http://gateway/v1",
                       model_api_key="temporary")
    additions = backend.sandbox_additions(ctx)
    path = Path(ctx.agent_home_host) / "sca-overlay.json"
    if overlay is None:
        assert additions.env == {"HOME": "/agent_home"}
        assert not path.exists()
    else:
        assert additions.env["SCA_AGENT_OVERLAY"] == "/agent_home/sca-overlay.json"
        assert json.loads(path.read_text()) == overlay.model_dump(exclude_none=True)
        assert path.stat().st_mode & 0o777 == 0o444
        assert hashlib.sha256(path.read_bytes()).hexdigest() == overlay.sha256()
        outside = tmp_path / "outside"
        outside.write_text("untouched")
        path.unlink()
        path.symlink_to(outside)
        backend.sandbox_additions(ctx)
        assert outside.read_text() == "untouched"
        assert not path.is_symlink()

    class Sandbox:
        async def exec(self, sid, argv, **kwargs):
            env = kwargs["env"]
            assert ("SCA_AGENT_OVERLAY" in env) == (overlay is not None)
            if overlay is not None:
                assert env["SCA_AGENT_OVERLAY"] == "/agent_home/sca-overlay.json"
            yield ExecOutput(stream="stdout", text=json.dumps({
                "type": "step_finish", "part": {"reason": "stop"}}))
            yield ExecExit(exit_code=0)

    events = [event async for event in backend.run_turn(
        Sandbox(), "sid", ctx, "prompt", session_id=None, timeout_s=5)]
    assert events[-1].type == "turn_completed"


@pytest.mark.parametrize("backend", ["codex", "fake", "pi", "opencode-brain", "opencode"])
def test_context_rejects_unsupported_backend(tmp_path, backend):
    acts = object.__new__(KernelActivities)
    acts.settings = Settings(_env_file=None, data_dir=tmp_path)
    acts.agent = SimpleNamespace(name=backend)
    acts.backends = {backend: acts.agent}
    spec = CodingSpec(repo_url="r", instruction="i", agent_overlay={"system_prompt": "custom"})
    if backend == "opencode":
        assert acts._agent_ctx("id", spec).agent_overlay == spec.agent_overlay
    else:
        with pytest.raises(ApplicationError, match="only supported by opencode") as error:
            acts._agent_ctx("id", spec)
        assert error.value.non_retryable
        assert error.value.type == "InvalidTaskError"
    assert acts._agent_ctx("id", spec.model_copy(update={"agent_overlay": None})).agent_overlay is None


def git(*args, cwd=None):
    return subprocess.run(["timeout", "30", "git", *map(str, args)], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_patch_applies(tmp_path):
    source = Path(".data/opencode/src").resolve()
    if not source.is_dir():
        pytest.skip("OpenCode source cache is absent")
    work = tmp_path / "source"
    work.mkdir()
    archive = tmp_path / "source.tar"
    git("-C", source, "archive", "-o", archive, "HEAD")
    subprocess.run(["timeout", "30", "tar", "-xf", str(archive), "-C", str(work)], check=True)
    git("init", "-q", "-b", "main", cwd=work)
    patch = Path("deploy/opencode/sca-overlay.patch").resolve()
    git("apply", "--check", patch, cwd=work)


async def test_upgrade_idempotent(tmp_path, capsys):
    source = Path(".data/opencode/src").resolve()
    if not source.is_dir():
        pytest.skip("OpenCode source cache is absent")
    cfg = Settings(_env_file=None, data_dir=tmp_path)
    bare = Path(cfg.opencode_repo_url)
    bare.parent.mkdir(parents=True)
    git("clone", "--bare", "--single-branch", source, bare)
    git("--git-dir", bare, "branch", "-f", "main", "HEAD")
    before = git("--git-dir", bare, "rev-parse", "main")
    upgrade = runpy.run_path("scripts/opencode_overlay_upgrade.py")["upgrade"]
    upgrade.__globals__["get_settings"] = lambda: cfg
    await upgrade()
    after = git("--git-dir", bare, "rev-parse", "main")
    assert before != after
    assert git("--git-dir", bare, "rev-parse", "main^") == before
    assert git("--git-dir", bare, "show", "-s", "--format=%s", "main") == (
        "Allow runtime prompt and tool overlays")
    await upgrade()
    assert git("--git-dir", bare, "rev-parse", "main") == after
    assert "/v1/agents/opencode/builds" in capsys.readouterr().out


@pytest.mark.parametrize("overlay", [None, {}, {"system_prompt": "private overlay text"}])
async def test_turn_records_only_overlay_hash(tmp_path, monkeypatch, overlay):
    from sca.kernel.activities import TurnInput

    acts = object.__new__(KernelActivities)
    acts.settings = Settings(_env_file=None, data_dir=tmp_path)
    acts.agent = SimpleNamespace(name="opencode")
    acts.backends = {"opencode": acts.agent}
    acts.gateway = None
    spec = CodingSpec(repo_url="r", instruction="i", agent_overlay=overlay)
    updates = []
    events = []

    async def noop(*args):
        return None

    async def get_spec(*args):
        return spec

    async def update(task_id, data):
        updates.append(data)

    class Recorded(Exception):
        pass

    async def append(task_id, event_type, payload, **kwargs):
        events.append((event_type, payload))
        raise Recorded

    monkeypatch.setattr("sca.kernel.activities.activity.info", lambda: SimpleNamespace(attempt=1))
    acts._ensure_state = noop
    acts._spec = get_spec
    acts._sandbox_id = noop
    acts._before_agent_turn = noop
    acts._activity_route = dict
    acts.store = SimpleNamespace(get_session_id=noop, update_result=update, append_event=append)
    with pytest.raises(Recorded):
        await acts._run_agent_turn(TurnInput(task_id="id", turn=1, prompt="task prompt"))
    kind, payload = events[0]
    assert kind == "agent.turn_started"
    if overlay is None:
        assert updates == []
        assert "overlay_sha256" not in payload
    else:
        digest = AgentOverlay.model_validate(overlay).sha256()
        assert updates == [{"overlay_sha256": digest}]
        assert payload["overlay_sha256"] == digest
        assert "private overlay text" not in json.dumps(payload)


async def test_fresh_setup_preserves_planted_commit(tmp_path, capsys):
    source = Path(".data/opencode/src").resolve()
    if not source.is_dir():
        pytest.skip("OpenCode source cache is absent")
    cfg = Settings(_env_file=None, data_dir=tmp_path, opencode_src_cache=source)
    setup = runpy.run_path("scripts/opencode_setup.py")["setup"]
    setup.__globals__["get_settings"] = lambda: cfg
    await setup(False)
    bare = Path(cfg.opencode_repo_url)
    assert git("--git-dir", bare, "rev-list", "--count", "main") == "3"
    assert git("--git-dir", bare, "show", "-s", "--format=%s", "main^") == (
        "Normalize text written by file tools")
    assert git("--git-dir", bare, "show", "-s", "--format=%s", "main") == (
        "Allow runtime prompt and tool overlays")
    assert git("--git-dir", bare, "diff", "main^", "main", "--",
               "packages/core/src/file-write.ts") == ""
    assert "Docker execution deferred" in capsys.readouterr().out
