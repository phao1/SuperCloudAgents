import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from sca.agents.dsh import KEY_ENV, LAUNCH, OVERLAY_FILENAME, OVERLAY_MODULE, DshBackend
from sca.config import Settings
from sca.core.errors import AgentTurnError, SandboxError
from sca.core.models import (
    AgentContext,
    AgentEvent,
    AgentOverlay,
    ExecEvent,
    ExecExit,
    ExecOutput,
    SandboxSpec,
)
from sca.host import host_executable
from sca.kinds.coding.spec import CodingSpec
from sca.orchestration.worker import make_agent_backend
from sca.sandbox.local import LocalSandboxProvider

SESSION = "session-8b20b100-007a-4a19-927a-c1e1d1db1e00"
KEY = "fixture-virtual-key"


def stdout(**record) -> ExecOutput:
    return ExecOutput(stream="stdout", text=json.dumps(record))


def opening(session: str = SESSION) -> ExecOutput:
    return stdout(type="session", sessionId=session, cwd="/workspace")


def final(session: str = SESSION, text: str = "") -> ExecOutput:
    return stdout(type="final", sessionId=session, reason={"kind": "completed"}, text=text)


class Sandbox:
    def __init__(self, *output: ExecEvent, error: SandboxError | None = None) -> None:
        self.output = output
        self.error = error
        self.calls = []

    async def exec(self, sandbox_id: str, argv: list[str], **kwargs) -> AsyncIterator[ExecEvent]:
        self.calls.append({"sandbox_id": sandbox_id, "argv": argv, **kwargs})
        for event in self.output:
            yield event
        if self.error is not None:
            raise self.error


@pytest.fixture
def ctx(tmp_path) -> AgentContext:
    return AgentContext(task_id="task", agent_home_host=str(tmp_path / "home"),
                        model_base_url="http://gateway/v1/", model_api_key=KEY)


async def collect(backend: DshBackend, sandbox: Sandbox, ctx: AgentContext,
                  session_id: str | None = None) -> list[AgentEvent]:
    return [e async for e in backend.run_turn(sandbox, "s", ctx, "修改测试", session_id=session_id, timeout_s=30)]


@pytest.mark.parametrize("transport", ["mount", "sync"])
def test_home_and_image(ctx, transport):
    config = Settings(_env_file=None, agent_backend="dsh")
    backend = make_agent_backend(config)
    assert isinstance(backend, DshBackend)
    assert backend.version == config.dsh_version == "0.2.2-rc.5"
    assert backend.sandbox_image == config.dsh_sandbox_image == "sca-sandbox-dsh:latest"
    backend.state_transport = transport
    additions = backend.sandbox_additions(ctx)
    assert additions.env == {"HOME": "/agent_home", "DSH_HOME": "/agent_home/dsh"}
    if transport == "mount":
        mount, = additions.mounts
        assert mount.source == ctx.agent_home_host and mount.target == "/agent_home"
        assert mount.read_only is False
    else:
        assert additions.mounts == []


@pytest.mark.parametrize(("limiter", "setting", "protocol"), [
    (False, "auto", "messages"), (True, "auto", "openai"),
    (False, "openai", "openai"), (True, "messages", "messages"),
])
def test_worker_protocol_selection(limiter, setting, protocol):
    spec = CodingSpec(repo_url="/repo", instruction="test", agent_backend="dsh")
    config = Settings(_env_file=None, agent_backend=spec.agent_backend, gateway_limiter=limiter,
                      dsh_gateway_protocol=setting, default_model="deepseek-flash")
    backend = make_agent_backend(config)
    assert isinstance(backend, DshBackend) and backend.protocol == protocol


@pytest.mark.parametrize("protocol", ["messages", "openai"])
async def test_command_env_and_session_continuity(ctx, protocol):
    backend = DshBackend("deepseek-flash", protocol=protocol)
    first = Sandbox(opening(), final(), ExecExit(exit_code=0))
    context = ctx.model_copy(update={"model": "deepseek-v4-pro", "model_base_url": "http://gateway"})
    events = await collect(backend, first, context)
    assert [event.type for event in events] == ["session_started", "turn_completed"]
    assert events[0].data == {"session_id": SESSION}
    assert events[1].data == {"final_message": ""}
    (call,) = first.calls
    patch_file = "/agent_home/sca-dsh.patch.yml"
    argv = ["python3", "-c", LAUNCH, patch_file, OVERLAY_FILENAME, "", "--", "dsh",
            "--profile", "headless", "--patch", patch_file,
            "--json", "--permission-mode", "workspace-write", "--approval-policy", "never"]
    assert call["argv"] == [*argv, "-"]
    patch, prompt = call["stdin"].split(b"\n", 1)
    assert prompt == "修改测试".encode() and call["timeout_s"] == 30
    assert call["workdir"] == "/workspace" and call["sandbox_id"] == "s"
    env = call["env"].copy()
    assert env == {
        "HOME": "/agent_home", "DSH_HOME": "/agent_home/dsh", "DSH_PERMISSION_MODE": "workspace-write",
        "DSH_TELEMETRY_MODE": "DISABLED", "DEEPSEEK_BASE_URL": "http://gateway/v1", KEY_ENV: KEY,
    }
    assert KEY.encode() not in patch and KEY not in str(call["argv"])
    rows = {row["id"]: row for row in json.loads(patch)}
    for plugin in ("tool-web", "web-search-deepseek", "web-deepseek-api", "web-fetch-http"):
        assert rows[plugin]["disabled"] is True
    selection = rows["agent-default-model"]["config"]
    assert selection["model"] == "deepseek-v4-pro"
    if protocol == "messages":
        assert selection["provider"] == "deepseek-official"
        assert rows["llm-deepseek"]["config"] == {"baseURL": "http://gateway/v1", "apiKeyEnv": KEY_ENV}
        assert rows["llm-pi-ai"]["disabled"] is True
    else:
        provider = rows["llm-pi-ai"]["config"]["providers"][selection["provider"]]
        assert provider["api"] == "openai-completions" and provider["baseURL"] == "http://gateway/v1"
        assert provider["apiKeyEnv"] == KEY_ENV and provider["models"] == [{"id": "deepseek-v4-pro"}]
        assert provider["defaultMaxTokens"] < Settings(_env_file=None).gateway_key_tpm
        assert rows["llm-deepseek"]["disabled"] is True

    second = Sandbox(opening(), final(text="same session"), ExecExit(exit_code=0))
    next_context = context.model_copy(update={"model_api_key": "fixture-rotated-key"})
    resumed = await collect(backend, second, next_context, events[0].data["session_id"])
    assert [e.type for e in resumed] == ["turn_completed"]
    assert second.calls[0]["argv"] == [*argv, "--session-id", SESSION, "-"]
    assert second.calls[0]["env"][KEY_ENV] == next_context.model_api_key
    assert second.calls[0]["stdin"] == call["stdin"]


@pytest.mark.parametrize("shell", ["bash", "pwsh"])
async def test_parses_headless_events_and_redacts_nested_payloads(ctx, shell):
    # Synthetic projection from @deepseek-ai/dsh-headless src/json-stream.ts.
    encoded_key = r"\u0066ixture-virtual-key"
    call = stdout(type="tool_call", callId="cmd1", tool=shell, input={
        "command": "echo " + KEY, "meta": [{KEY: KEY}],
    })
    call.text = call.text.replace(KEY, encoded_key)
    sandbox = Sandbox(
        ExecOutput(stream="stdout", text="not JSON"), ExecOutput(stream="stdout", text="[]"),
        opening(), stdout(type="thinking", text="unpublished"), stdout(type=["unknown"]),
        stdout(type="status", phase="step_start", turn=1, step=1),
        stdout(type="text", text="Testing " + KEY), call,
        stdout(type="tool_result", callId="cmd1", status="completed", result="x" * 5000 + KEY),
        stdout(type="tool_call", callId="edit1", tool="edit", input={"file_path": "demo.py"}),
        stdout(type="tool_result", callId="edit1", status="completed", result="Edited"),
        stdout(type="tool_result", callId="unknown", status="error", result="denied", error={"reason": KEY}),
        stdout(type="tool_call", truncated=True),
        stdout(type="status", phase="step_end", turn=1, step=1, usage={
            "inputTokens": 8, "outputTokens": 2, "cacheReadTokens": 3, "cacheWriteTokens": 1,
        }),
        stdout(type="status", phase="step_end", turn=1, step=2),
        stdout(type="status", phase="step_end", turn=1, step=3, usage={"inputTokens": True, "outputTokens": 7}),
        stdout(type="status", phase="turn_end", turn=1, reason={"kind": "completed"}),
        final(text="Final " + KEY), ExecExit(exit_code=0),
    )
    events = await collect(DshBackend("deepseek-flash"), sandbox, ctx)
    assert [e.type for e in events] == ["session_started", "message", "tool", "command", "tool",
                                       "file_change", "tool", "usage", "turn_completed"]
    assert events[2].data["input"]["meta"] == [{"[REDACTED]": "[REDACTED]"}]
    assert events[3].data["name"] == shell
    assert events[3].data["command"] == "echo [REDACTED]" and events[3].data["exit_code"] is None
    assert events[3].data["output_tail"].startswith("…") and events[3].data["output_tail"].endswith("[REDACTED]")
    assert events[5].data["changes"] == [{"path": "demo.py", "kind": "update"}]
    assert events[6].data["is_error"] is True
    assert events[7].data == {"input_tokens": 12, "cached_input_tokens": 3, "output_tokens": 2}
    assert events[7].model_call_succeeded
    assert events[-1].data == {"final_message": "Final [REDACTED]"}
    assert KEY not in str(events)


@pytest.mark.parametrize(("events", "problem"), [
    ([], "missing exit status"),
    ([ExecExit(exit_code=0)], "missing final event"),
    ([final(), ExecExit(exit_code=1)], "exit code 1"),
    ([final(), ExecExit(exit_code=124, timed_out=True)], "timed out"),
    ([stdout(type="final", sessionId=SESSION, reason=None, text=""), ExecExit(exit_code=0)], "final reason missing"),
    ([stdout(type="final", sessionId=SESSION, reason={"kind": "aborted"}, text=""), ExecExit(exit_code=0)],
     "final reason aborted"),
    ([final(session="session-other"), ExecExit(exit_code=0)], "session ID mismatch"),
])
async def test_only_completed_final_with_zero_exit_succeeds(ctx, events, problem):
    with pytest.raises(AgentTurnError, match=problem):
        await collect(DshBackend("deepseek-flash"), Sandbox(opening(), *events), ctx)


async def test_failed_driver_diagnosis_has_last_error_and_stderr_tail(ctx):
    sandbox = Sandbox(
        stdout(type="error", message="first error"),
        ExecOutput(stream="stderr", text="discard this prefix " + "x" * 5000),
        stdout(type="error", message=f"last error {KEY}"),
        ExecOutput(stream="stderr", text=f"tail message {KEY}"), ExecExit(exit_code=1),
    )
    with pytest.raises(AgentTurnError) as caught:
        await collect(DshBackend("deepseek-flash"), sandbox, ctx, SESSION)
    diagnosis = str(caught.value)
    assert "exit code 1" in diagnosis
    assert 'last error event {"type": "error", "message": "last error [REDACTED]"}' in diagnosis
    assert "stderr tail …" in diagnosis and "tail message [REDACTED]" in diagnosis
    assert all(text not in diagnosis for text in (KEY, "first error", "discard this prefix"))


@pytest.mark.parametrize(("status", "code", "kind"), [
    (402, "QUOTA", "billing"), (401, "AUTH", "auth"), (429, "RATE_LIMIT", "rate_limit"),
    (None, "QUOTA", "billing"), (None, "AUTH", "auth"), (None, "RATE_LIMIT", "rate_limit"),
])
async def test_classifies_provider_failure(ctx, status, code, kind):
    reason = {"kind": "error", "error": {"code": code, "message": f"Rejected {KEY}"}}
    if status is not None:
        reason["error"]["status"] = status
    sandbox = Sandbox(opening(), stdout(type="status", phase="turn_end", reason=reason),
                      stdout(type="final", sessionId=SESSION, reason=reason, text=""), ExecExit(exit_code=1))
    backend = DshBackend("deepseek-flash")
    events = []
    with pytest.raises(AgentTurnError, match="exit code 1") as caught:
        async for event in backend.run_turn(sandbox, "s", ctx, "task", session_id=None, timeout_s=30):
            events.append(event)
    assert [event.type for event in events] == ["session_started", "error"]
    payload = events[-1].data["model_provider_error"]
    assert payload["kind"] == kind and payload["status"] == status
    assert KEY not in str(events) + str(caught.value)


async def test_stderr_only_failure_is_classified(ctx):
    sandbox = Sandbox(ExecOutput(stream="stderr", text=f"dsh: HTTP 429 {KEY}"), ExecExit(exit_code=1))
    events = []
    with pytest.raises(AgentTurnError) as caught:
        async for event in DshBackend("deepseek-flash").run_turn(
            sandbox, "s", ctx, "task", session_id=None, timeout_s=30,
        ):
            events.append(event)
    assert events[0].data["model_provider_error"]["kind"] == "rate_limit"
    assert "HTTP 429 [REDACTED]" in str(caught.value) and KEY not in str(caught.value)


async def test_gateway_budget_failure_keeps_gateway_classification(ctx):
    reason = {"kind": "error", "error": {"code": "QUOTA", "message": "Budget exceeded"}}
    sandbox = Sandbox(opening(), stdout(type="final", sessionId=SESSION, reason=reason, text=""),
                      ExecExit(exit_code=1))
    events = []
    with pytest.raises(AgentTurnError, match="model gateway budget refused"):
        async for event in DshBackend("deepseek-flash").run_turn(
            sandbox, "s", ctx, "task", session_id=None, timeout_s=30,
        ):
            events.append(event)
    assert events[-1].data == {"message": "model gateway budget refused"}


async def test_sandbox_error_keeps_key_out_of_diagnosis(ctx):
    with pytest.raises(AgentTurnError, match=r"could not start \[REDACTED\]") as caught:
        await collect(DshBackend("deepseek-flash"), Sandbox(error=SandboxError(f"could not start {KEY}")), ctx)
    assert KEY not in str(caught.value)


async def test_invalid_context_and_mismatched_resume_are_refused(ctx):
    backend = DshBackend("deepseek-flash")
    sandbox = Sandbox(opening(), final(), ExecExit(exit_code=0))
    with pytest.raises(AgentTurnError, match="requires model_base_url and model_api_key"):
        await collect(backend, sandbox, ctx.model_copy(update={"model_api_key": None}))
    with pytest.raises(AgentTurnError, match="version is not installed"):
        await collect(backend, sandbox, ctx.model_copy(update={"agent_version": "wrong"}))
    with pytest.raises(AgentTurnError, match="Invalid DSH session ID"):
        await collect(backend, sandbox, ctx, "../other")
    assert sandbox.calls == []
    with pytest.raises(AgentTurnError, match="session"):
        await collect(backend, sandbox, ctx, "session-other")


@pytest.fixture
def launcher_env(tmp_path) -> dict[str, str]:
    env = {"TMPDIR": str(tmp_path), "PYTHONUTF8": "1"}
    if sys.platform == "win32":
        env["SystemRoot"] = os.environ["SystemRoot"]
    return env


@pytest.mark.parametrize("exit_code", [0, 7])
def test_launcher_preserves_stdin_and_writes_only_patch(tmp_path, launcher_env, exit_code):
    patch = tmp_path / "sca-dsh.patch.yml"
    stub = """import os, sys
assert os.environ['DEEPSEEK_API_KEY']
assert 'SCA_DSH_PATCH' not in os.environ
sys.stdout.buffer.write(sys.stdin.buffer.read())
sys.exit(int(sys.argv[1]))
"""
    result = subprocess.run(
        [sys.executable, "-c", LAUNCH, str(patch), OVERLAY_FILENAME, "", "--",
         sys.executable, "-c", stub, str(exit_code),
         "--profile", "headless", "--patch", str(patch), "-"],
        input=b"[]\nprompt\n", capture_output=True, check=False, timeout=5,
        env={**launcher_env, KEY_ENV: KEY},
    )
    assert result.returncode == exit_code
    assert result.stdout == b"prompt\n" and result.stderr == b""
    assert patch.read_text(encoding="utf-8") == "[]"


async def test_overlay_launcher_writes_literal_guidance_outside_workspace(tmp_path, ctx, launcher_env):
    home, workdir = tmp_path / "agent home", tmp_path / "user repo"
    home.mkdir()
    workdir.mkdir()
    prompt = "Keep {{literal_template}} verbatim.\n" + "字" * 24_000
    context = ctx.model_copy(update={"agent_overlay": AgentOverlay(
        system_prompt=prompt, tool_guidance="Verify edits", tool_descriptions={"read": "Read context first"},
    )})
    sandbox = Sandbox(opening(), final(), ExecExit(exit_code=0))
    await collect(DshBackend("deepseek-flash"), sandbox, context)
    call, = sandbox.calls
    assert len(call["stdin"]) > 128_000
    assert prompt not in str(call["argv"]) + str(call["env"])
    assert KEY.encode() not in call["stdin"]
    assert call["argv"][call["argv"].index("--permission-mode") + 1] == "workspace-write"

    read_patch = (
        "import json, os, sys; from pathlib import Path; "
        "path = Path(sys.argv[sys.argv.index('--patch') + 1]); "
        "print(json.dumps([str(path), os.environ['DSH_HOME'], sys.stdin.read()]))"
    )
    path = str(home / "sca-dsh.patch.yml")
    argv = [sys.executable, "-c", LAUNCH, path, OVERLAY_FILENAME, OVERLAY_MODULE, "--",
            sys.executable, "-c", read_patch, "--profile", "headless", "--patch", path, "-"]
    result = await asyncio.to_thread(
        subprocess.run, argv, cwd=workdir, env={**call["env"], **launcher_env},
        input=call["stdin"], capture_output=True, check=True, timeout=5,
    )
    filename, dsh_home, task = json.loads(result.stdout)
    patch = Path(filename)
    rows = json.loads(patch.read_text(encoding="utf-8"))
    plugin, = rows[-1]["insert"]
    assert plugin["name"] == f"./{OVERLAY_FILENAME}"
    assert plugin["config"]["guidance"] == prompt + "\n\nVerify edits\n\nAdditional tool guidance:\n- read: Read context first"
    assert (patch.parent / OVERLAY_FILENAME).read_text(encoding="utf-8") == OVERLAY_MODULE
    assert dsh_home == str(home / "dsh") and task == "修改测试"
    assert patch.parent.parent == tmp_path and list(workdir.iterdir()) == []


def test_task_spec_accepts_dsh_npm_versions_only_for_dsh():
    spec = {"repo_url": "/repo", "instruction": "test", "agent_version": "0.2.2-rc.5"}
    assert CodingSpec.model_validate({**spec, "agent_backend": "dsh"}).agent_version == spec["agent_version"]
    for backend in ("opencode", None):
        with pytest.raises(ValidationError, match="agent_version"):
            CodingSpec.model_validate({**spec, "agent_backend": backend})
    with pytest.raises(ValidationError, match="agent_version"):
        CodingSpec.model_validate({**spec, "agent_backend": "dsh", "agent_version": "../anything"})


@pytest.mark.parametrize("guidance", [None, "保留 {{literal}}。\nVerify edits."], ids=["baseline", "overlay"])
async def test_source_launcher_uses_task_home_and_same_gateway_env(tmp_path, ctx, guidance):
    node = host_executable("node.exe" if sys.platform == "win32" else "node")
    if node is None:
        pytest.skip("Node.js is not installed")
    binary = tmp_path / "dsh cli.mjs"
    binary.write_text("""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
const env = process.env;
assert.equal(env.DEEPSEEK_BASE_URL, 'http://gateway/v1');
assert.ok(env.DEEPSEEK_API_KEY);
assert.equal(env.DSH_HOME, resolve(env.HOME, 'dsh'));
assert.equal(env.DSH_TELEMETRY_MODE, 'DISABLED');
assert.equal(env.DSH_PERMISSION_MODE, 'workspace-write');
assert.equal(env.SCA_DSH_PATCH, undefined);
const patch = process.argv[process.argv.indexOf('--patch') + 1];
assert.ok(JSON.parse(readFileSync(patch, 'utf8')).length > 0);
const sessionId = 'session-8b20b100-007a-4a19-927a-c1e1d1db1e00';
console.log(JSON.stringify({type: 'session', sessionId}));
console.log(JSON.stringify({type: 'text', text: JSON.stringify({patch, task: readFileSync(0, 'utf8')})}));
console.log(JSON.stringify({type: 'final', sessionId, reason: {kind: 'completed'}, text: '完成'}));
""", encoding="utf-8")
    sandbox = LocalSandboxProvider(tmp_path / "sandboxes")
    backend = DshBackend("deepseek-flash", executable=str(binary), node_executable=str(node))
    assert backend.sandbox_image is None
    assert backend.state_transport == "sync"
    assert backend.python_executable == sys.executable
    additions = backend.sandbox_additions(ctx)
    assert not additions.mounts
    sandbox_id = await sandbox.create("s", SandboxSpec(image="unused", env=additions.env))
    task = "修改测试\r\nKeep {{literal}}.\n\n"
    if guidance is not None:
        ctx = ctx.model_copy(update={"agent_overlay": AgentOverlay(system_prompt=guidance)})
    try:
        events = [event async for event in backend.run_turn(
            sandbox, sandbox_id, ctx, task, session_id=None, timeout_s=30,
        )]
        assert [event.type for event in events] == ["session_started", "message", "turn_completed"]
        assert events[-1].data["final_message"] == "完成"
        received = json.loads(events[1].data["text"])
        assert received["task"] == task
        patch = Path(received["patch"])
        directory = sandbox.root / sandbox_id
        if guidance is None:
            assert patch == directory / "agent_home" / "sca-dsh.patch.yml"
        else:
            assert patch.parent.parent == directory / "tmp"
            rows = json.loads(patch.read_text(encoding="utf-8"))
            plugin, = rows[-1]["insert"]
            assert plugin["name"] == f"./{OVERLAY_FILENAME}"
            assert plugin["config"]["guidance"] == guidance
            assert (patch.parent / OVERLAY_FILENAME).read_text(encoding="utf-8") == OVERLAY_MODULE
    finally:
        await sandbox.destroy(sandbox_id)
