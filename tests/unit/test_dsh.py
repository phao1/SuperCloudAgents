import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, RetryState

from sca.agents import docker_node
from sca.agents.docker_node import NODE_IMAGE, DockerNodeRuntime
from sca.agents.dsh import KEY_ENV, LAUNCH, OVERLAY_FILENAME, OVERLAY_MODULE, DshBackend
from sca.config import Settings
from sca.core.errors import (
    MODEL_BUDGET_EXHAUSTED,
    MODEL_BUDGET_MISCONFIGURED,
    AgentTurnError,
    SandboxError,
)
from sca.core.models import (
    AgentContext,
    AgentEvent,
    AgentOverlay,
    ExecEvent,
    ExecExit,
    ExecOutput,
    Mount,
    SandboxSpec,
)
from sca.gateway.litellm import LiteLLMGateway
from sca.host import host_executable
from sca.kernel.activities import KernelActivities, TurnInput
from sca.kernel.workflow import TaskWorkflow
from sca.kinds.coding.spec import CodingSpec
from sca.orchestration.worker import make_agent_backend
from sca.sandbox._exec import NODE_EXECUTABLE, NODE_RUNTIME_DIR, NODE_RUNTIME_LABEL
from sca.sandbox.docker import DockerSandboxProvider
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
def test_worker_protocol_selection(limiter, setting, protocol, monkeypatch):
    monkeypatch.setenv("SCA_DSH_MAX_OUTPUT_TOKENS", "6144")
    spec = CodingSpec(repo_url="/repo", instruction="test", agent_backend="dsh")
    config = Settings(_env_file=None, agent_backend=spec.agent_backend, gateway_limiter=limiter,
                      dsh_gateway_protocol=setting, default_model="deepseek-flash")
    backend = make_agent_backend(config)
    assert isinstance(backend, DshBackend) and backend.protocol == protocol
    assert backend.max_output_tokens == 6144


def test_dsh_output_cap_must_be_positive(monkeypatch):
    monkeypatch.setenv("SCA_DSH_MAX_OUTPUT_TOKENS", "0")
    with pytest.raises(ValidationError, match="dsh_max_output_tokens"):
        Settings(_env_file=None)


@pytest.mark.parametrize("protocol", ["messages", "openai"])
@pytest.mark.parametrize("override", [None, 6144])
async def test_command_env_and_session_continuity(ctx, protocol, override):
    options = {"max_output_tokens": override} if override is not None else {}
    backend = DshBackend("deepseek-flash", protocol=protocol, **options)
    cap = override or 8192
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
    for plugin in ("tool-web", "web-search-deepseek", "web-deepseek-api", "web-fetch-http", "session-title-llm"):
        assert rows[plugin]["disabled"] is True
    assert rows["compaction-basic"]["config"]["maxTokens"] == cap
    selection = rows["agent-default-model"]["config"]
    assert selection["model"] == "deepseek-v4-pro"
    if protocol == "messages":
        assert selection["provider"] == "deepseek-official"
        assert rows["llm-deepseek"]["config"] == {
            "baseURL": "http://gateway/v1", "apiKeyEnv": KEY_ENV, "maxTokens": cap,
        }
        assert rows["llm-pi-ai"]["disabled"] is True
    else:
        provider = rows["llm-pi-ai"]["config"]["providers"][selection["provider"]]
        assert provider["api"] == "openai-completions" and provider["baseURL"] == "http://gateway/v1"
        assert provider["apiKeyEnv"] == KEY_ENV
        assert provider["models"] == [{"id": "deepseek-v4-pro", "maxTokens": cap}]
        assert provider["compat"]["maxTokensField"] == "max_tokens"
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
    assert [e.type for e in events] == ["session_started", "activity", "message", "tool", "command", "tool",
                                       "file_change", "tool", "usage", "turn_completed"]
    assert events[1].data["operation"] == "model"
    assert events[3].data["input"]["meta"] == [{"[REDACTED]": "[REDACTED]"}]
    assert events[4].data["name"] == shell
    assert events[4].data["command"] == "echo [REDACTED]" and events[4].data["exit_code"] is None
    assert events[4].data["output_tail"].startswith("…") and events[4].data["output_tail"].endswith("[REDACTED]")
    assert events[6].data["changes"] == [{"path": "demo.py", "kind": "update"}]
    assert events[7].data["is_error"] is True
    assert events[8].data == {"input_tokens": 12, "cached_input_tokens": 3, "output_tokens": 2}
    assert events[8].model_call_succeeded
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


@pytest.mark.parametrize(("progress", "prior_spend", "expected"), [
    ("none", 0, MODEL_BUDGET_MISCONFIGURED),
    ("zero_usage", 0, MODEL_BUDGET_MISCONFIGURED),
    ("usage", 0, MODEL_BUDGET_EXHAUSTED),
    ("tool", 0, MODEL_BUDGET_EXHAUSTED),
    ("none", 304, MODEL_BUDGET_EXHAUSTED),
])
async def test_dsh_gateway_budget_failure_reaches_task_state(tmp_path, monkeypatch, progress, prior_spend, expected):
    model_events = {
        "none": [],
        "zero_usage": [stdout(type="status", phase="step_end", usage={"inputTokens": 0, "outputTokens": 0})],
        "usage": [stdout(type="status", phase="step_end", usage={"inputTokens": 561, "cacheReadTokens": 5248,
                                                                  "outputTokens": 87})],
        "tool": [stdout(type="tool_call", callId="glob-1", tool="glob", input={"pattern": "*.py"})],
    }
    refusal = {"kind": "error", "error": {"code": "HTTP_422", "message": f"Budget has been exceeded! Key={KEY}"}}
    sandbox = Sandbox(opening(), *model_events[progress],
                      stdout(type="final", sessionId=SESSION, reason=refusal, text=""), ExecExit(exit_code=1))
    store = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(kind="coding", usage={"cost_micro_usd": prior_spend})),
        get_session_id=AsyncMock(return_value=None), set_session=AsyncMock(),
        append_event=AsyncMock(), add_usage=AsyncMock(),
    )
    gateway = SimpleNamespace(sandbox_url="http://gateway/v1", issue_key=AsyncMock(return_value=KEY),
                              finalize_key=AsyncMock(return_value=(0, True)))
    kernel = KernelActivities(settings=Settings(_env_file=None, data_dir=tmp_path), store=store,
                              sandbox=sandbox, agent=DshBackend("deepseek-flash"), objects=None, gateway=gateway)
    kernel._ensure_state = AsyncMock(return_value=False)
    kernel._spec = AsyncMock(return_value=CodingSpec(repo_url="/repo", instruction="test"))
    kernel._sandbox_id = AsyncMock(return_value="sandbox")
    monkeypatch.setattr("sca.kernel.activities.activity.info", lambda: SimpleNamespace(attempt=1))
    monkeypatch.setattr("sca.kernel.activities.activity.heartbeat", lambda: None)

    states, budget_checks = [], []

    async def execute(name, arg, **kwargs):
        match name:
            case "load_task":
                return {"max_fix_attempts": 0, "turn_timeout_s": 5,
                        "hooks": {"prepare": "prepare", "initial_prompt": "initial_prompt"}}
            case "initial_prompt":
                return "prompt"
            case "check_budget":
                budget_checks.append(arg.turn)
                return "Task cost budget exhausted" if arg.turn else None
            case "set_state":
                states.append(arg)
            case "run_agent_turn":
                with pytest.raises(ApplicationError) as failure:
                    await kernel._run_agent_turn(arg)
                assert failure.value.non_retryable and failure.value.type == "BudgetExceeded"
                raise ActivityError(
                    "Activity failed", scheduled_event_id=1, started_event_id=2,
                    activity_type=name, activity_id="1", identity="test",
                    retry_state=RetryState.NON_RETRYABLE_FAILURE,
                ) from failure.value
            case "prepare" | "create_sandbox" | "destroy_sandbox":
                return None
            case _:
                pytest.fail(f"Unexpected activity {name}")

    monkeypatch.setattr(workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow, "patched", lambda _: True)
    with pytest.raises(ApplicationError, match=expected):
        await TaskWorkflow().run("task")
    assert states[-1].state == "FAILED" and states[-1].reason == expected
    assert budget_checks == [0]
    gateway.finalize_key.assert_awaited_once()
    events = [(call.args[1], call.args[2]) for call in store.append_event.await_args_list]
    assert events[-1] == ("budget.exceeded", {"turn": 1, "reason": expected})
    assert any(payload.get("message") == "model gateway budget refused" for _, payload in events)
    assert not any(kind in ("agent.turn_completed", "model.provider_error") for kind, _ in events)
    assert KEY not in str(events)


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


@pytest.mark.parametrize("arch,platform,oversized", [
    ("x64", "linux/amd64", False), ("arm64", "linux/arm64", False), ("x64", "linux/amd64", True),
])
async def test_docker_runtime_is_pinned_reused_and_prunes_unmounted_versions(
    tmp_path, monkeypatch, arch, platform, oversized,
):
    monkeypatch.delenv("SCA_NODE_RUNTIME_MAX_MB", raising=False)
    if oversized:
        monkeypatch.setenv("SCA_NODE_RUNTIME_MAX_MB", "1000")
    settings = Settings(_env_file=None, container_arch=arch, data_dir=tmp_path, docker_bin="fixture-docker")
    runtime = DockerNodeRuntime(settings, "dsh")
    runtime.root.mkdir(parents=True)
    stale, in_use = (runtime.root / name for name in ("dsh-old", "node-mounted"))
    stale.mkdir()
    in_use.mkdir()
    calls = []
    free_bytes = 2_000_000_000 if oversized else 3_000_000_000

    def fake_disk_usage(path):
        assert path == settings.data_dir.resolve()
        return SimpleNamespace(free=free_bytes)

    def fake_install(args, **kwargs):
        calls.append(args)
        assert args[0:2] == ["fixture-docker", "run"]
        assert args[args.index("--platform") + 1] == platform
        assert NODE_IMAGE in args and "--rm" in args
        assert args[-2:] == [f"@deepseek-ai/dsh@{settings.dsh_version}", "dsh"]
        assert "node_modules/.bin/dsh --profile headless --help" in args[args.index("-ec") + 1]
        stage = Path(args[args.index("--mount") + 1].split(",")[1].removeprefix("source="))
        node = stage / "node/bin/node"
        node.parent.mkdir(parents=True)
        node.touch(mode=0o700)
        modules = stage / "agent/node_modules"
        package = modules / "@deepseek-ai/dsh"
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"version": settings.dsh_version}))
        (modules / ".bin").mkdir()
        (modules / ".bin/dsh").touch()

    def fake_check_output(args, **kwargs):
        return "sandbox\n" if args[1] == "ps" else f"{in_use}\n"

    monkeypatch.setattr(docker_node.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(docker_node.os, "getgid", lambda: 1000, raising=False)
    monkeypatch.setattr(docker_node.subprocess, "run", fake_install)
    monkeypatch.setattr(docker_node.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(docker_node.shutil, "disk_usage", fake_disk_usage)
    monkeypatch.setattr(docker_node, "allocated_bytes", lambda *paths: 1_179_000_000)
    if oversized:
        with pytest.raises(SandboxError, match="超过磁盘上限.*占用 1179 MB，上限 1000 MB"):
            await runtime.mounts()
        assert stale.is_dir() and not runtime.agent.exists()
        assert not runtime.node.exists()
        assert not list(runtime.root.glob(".install-*"))
        return
    mounts = await runtime.mounts()
    free_bytes = 0
    assert mounts == await runtime.mounts()
    assert [mount.target for mount in mounts] == [NODE_RUNTIME_DIR, "/opt/sca-dsh"]
    assert all(mount.read_only for mount in mounts)
    assert (runtime.node / "bin/node").is_file()
    assert len(calls) == 1 and not stale.exists() and in_use.is_dir()
    assert not list(runtime.root.glob(".install-*"))


@pytest.mark.parametrize("backend,required_mb", [("dsh", 3000), ("pi", 1270)])
def test_docker_runtime_rejects_low_disk_before_install(tmp_path, monkeypatch, backend, required_mb):
    monkeypatch.delenv("SCA_NODE_RUNTIME_MAX_MB", raising=False)
    monkeypatch.delenv("SCA_PI_NODE_RUNTIME_MAX_MB", raising=False)
    settings = Settings(_env_file=None, data_dir=tmp_path / "data")
    runtime = DockerNodeRuntime(settings, backend)

    def fake_disk_usage(path):
        assert path == settings.data_dir.resolve() and path.is_dir()
        return SimpleNamespace(free=required_mb * 1_000_000 - 1)

    def unexpected_install(*args, **kwargs):
        pytest.fail("Docker installer started with insufficient disk space")

    monkeypatch.setattr(docker_node.shutil, "disk_usage", fake_disk_usage)
    monkeypatch.setattr(docker_node.subprocess, "run", unexpected_install)
    with pytest.raises(
        SandboxError, match=rf"{backend}.*需要至少 {required_mb} MB，可用 {required_mb - 1} MB",
    ):
        runtime.prepare()
    assert not list(runtime.root.glob(".install-*"))


async def test_kernel_mounts_dsh_in_the_requested_task_image(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, agent_backend="dsh", data_dir=tmp_path, pip_cache=False)
    backend = make_agent_backend(settings)
    monkeypatch.setattr(backend.docker_runtime, "prepare", lambda: None)
    sandbox = SimpleNamespace(create=AsyncMock(return_value="sandbox"))
    store = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(kind="coding", result={})), update_result=AsyncMock(),
    )
    kernel = KernelActivities(settings=settings, sandbox=sandbox, store=store, agent=backend, objects=None)
    kernel._ensure_state = AsyncMock()
    spec = CodingSpec(repo_url="/repo", instruction="test", image="python:3.12")
    kernel._spec = AsyncMock(return_value=spec)

    assert await kernel._create_sandbox("task") == ("sandbox", "python:3.12")
    spec = sandbox.create.await_args.args[-1]
    assert [mount.target for mount in spec.mounts] == ["/workspace", "/agent_home", NODE_RUNTIME_DIR, "/opt/sca-dsh"]
    assert all(mount.read_only for mount in spec.mounts[-2:])
    assert spec.network == "restricted"


@pytest.mark.parametrize("compatible", [True, False])
async def test_docker_probes_glibc_and_uses_node_for_setup(tmp_path, monkeypatch, compatible):
    from sca.sandbox import docker

    monkeypatch.setattr(docker.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(docker.os, "getgid", lambda: 1000, raising=False)
    provider = DockerSandboxProvider()
    provider._cli = AsyncMock(side_effect=[
        (1, "", "no such container"), (0, "", ""), (0 if compatible else 126, "", ""), (0, "", ""),
    ])
    spec = SandboxSpec(image="fixture", network="none", mounts=[
        Mount(source=str(tmp_path), target=NODE_RUNTIME_DIR, read_only=True),
    ])
    if not compatible:
        with pytest.raises(SandboxError, match="Alpine/musl"):
            await provider.create("task", spec)
        assert provider._cli.await_args.args == ("rm", "-f", "sca-task")
        return

    assert await provider.create("task", spec) == "sca-task"
    assert f"{NODE_RUNTIME_LABEL}=1" in provider._cli.await_args_list[1].args
    provider._node_exec.clear()  # A new worker recovers the interpreter from Docker metadata.
    provider._cli.reset_mock(side_effect=True)
    provider._cli.return_value = (0, "1\n", "")

    async def stream(command, cleanup, **options):
        args = command("wrapper", "/tmp/marker")
        assert args[args.index("sca-task") + 1:] == [
            NODE_EXECUTABLE, "--input-type=module", "-e", "wrapper", "--", "/tmp/marker", "bash", "-lc", "true",
        ]
        assert options["node"]
        await cleanup("cleanup", "/tmp/marker")
        yield ExecExit(exit_code=0)

    monkeypatch.setattr(docker, "stream_exec", stream)
    assert [event async for event in provider.exec("sca-task", ["bash", "-lc", "true"])] == [ExecExit(exit_code=0)]
    assert provider._cli.await_args.args[2] == NODE_EXECUTABLE


async def test_docker_node_launcher_preserves_overlay_stdin_and_session(tmp_path, ctx, launcher_env):
    node = host_executable("node.exe" if sys.platform == "win32" else "node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    settings = Settings(_env_file=None, agent_backend="dsh", data_dir=tmp_path)
    backend = make_agent_backend(settings)
    home = tmp_path / "home"
    home.mkdir()
    cli = tmp_path / "headless.mjs"
    cli.write_text(r"""
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
const args = process.argv;
const path = args[args.indexOf('--patch') + 1];
const patch = JSON.parse(readFileSync(path));
assert.equal(patch.at(-1).insert[0].config.guidance, 'Keep {{literal}}');
assert(existsSync(join(dirname(path), 'sca-dsh-overlay.mjs')));
assert(existsSync(dirname(process.env.DSH_HOME)));
assert.equal(args[args.indexOf('--permission-mode')+1], 'workspace-write');
assert.equal(args[args.indexOf('--approval-policy')+1], 'never');
const sessionId = args.includes('--session-id') ? args[args.indexOf('--session-id') + 1] : 'fixture-session';
console.log(JSON.stringify({type: 'session', sessionId}));
console.log(JSON.stringify({type: 'final', sessionId, reason: {kind: 'completed'}, text: readFileSync(0, 'utf8')}));
""", encoding="utf-8")

    class NodeSandbox:
        async def exec(self, sandbox_id, argv, *, env, stdin, **kwargs):
            assert argv[0] == NODE_EXECUTABLE and "python3" not in argv
            assert argv.count(NODE_EXECUTABLE) == 2
            mapped = [str(node) if arg == NODE_EXECUTABLE else str(cli) if arg == backend.docker_runtime.cli
                      else arg for arg in argv]
            process = await asyncio.create_subprocess_exec(
                *mapped, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env={**launcher_env, **env, "TEMP": str(tmp_path)},
            )
            out, err = await asyncio.wait_for(process.communicate(stdin), 10)
            for line in out.decode().splitlines():
                yield ExecOutput(stream="stdout", text=line)
            for line in err.decode(errors="replace").splitlines():
                yield ExecOutput(stream="stderr", text=line)
            yield ExecExit(exit_code=process.returncode)

    context = ctx.model_copy(update={"image": "debian:bookworm", "agent_home": str(home),
                                     "agent_overlay": AgentOverlay(tool_guidance="Keep {{literal}}")})
    sandbox = NodeSandbox()
    first = await collect(backend, sandbox, context)
    assert first[0].data["session_id"] == "fixture-session"
    assert first[-1].data["final_message"] == "修改测试"
    resumed = await collect(backend, sandbox, context, "fixture-session")
    assert [event.type for event in resumed] == ["turn_completed"]
    assert resumed[-1].data["final_message"] == "修改测试"


@pytest.mark.parametrize("limiter", [False, True])
async def test_successful_dsh_turn_records_virtual_key_spend_and_tokens(tmp_path, monkeypatch, limiter):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        match request.url.path:
            case "/key/list":
                keys = [{"token": "fixture-hash"}] if "/key/generate" in requests else []
                return httpx.Response(200, json={"keys": keys})
            case "/key/generate":
                assert json.loads(request.content)["models"] == ["deepseek-flash"]
                return httpx.Response(200, json={"key": KEY})
            case "/key/info":
                return httpx.Response(200, json={"info": {"spend": .0025}})
            case "/key/delete":
                return httpx.Response(200, json={})
        raise AssertionError(request.url.path)

    gateway = LiteLLMGateway("http://admin", "fixture-master", "http://gateway/v1")
    await gateway._http.aclose()
    gateway._http = httpx.AsyncClient(base_url="http://admin", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(gateway, "SETTLE_POLLS", 0)
    monkeypatch.setattr("sca.kernel.activities.activity.info", lambda: SimpleNamespace(attempt=1))
    monkeypatch.setattr("sca.kernel.activities.activity.heartbeat", lambda: None)
    settings = Settings(_env_file=None, agent_backend="dsh", data_dir=tmp_path,
                        default_model="deepseek-flash", gateway_limiter=limiter)
    store = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(kind="coding", usage={}, result={})),
        get_session_id=AsyncMock(return_value=None), set_session=AsyncMock(),
        append_event=AsyncMock(), add_usage=AsyncMock(),
    )
    sandbox = Sandbox(
        opening(), stdout(type="status", phase="step_end", usage={
            "inputTokens": 8, "outputTokens": 2, "cacheReadTokens": 3,
        }), final(text="Ready"), ExecExit(exit_code=0),
    )
    kernel = KernelActivities(settings=settings, sandbox=sandbox, store=store,
                              agent=make_agent_backend(settings), objects=None, gateway=gateway)
    kernel._ensure_state = AsyncMock(return_value=False)
    kernel._spec = AsyncMock(return_value=CodingSpec(repo_url="/repo", instruction="test"))
    kernel._sandbox_id = AsyncMock(return_value="sandbox")
    try:
        result = await kernel._run_agent_turn(TurnInput("task", 1, "prompt"))
    finally:
        await gateway.aclose()
    assert result.final_message == "Ready"
    assert [call.args[1] for call in store.add_usage.await_args_list] == [
        {"input_tokens": 11, "cached_input_tokens": 3, "output_tokens": 2}, {"cost_micro_usd": 2500},
    ]
    assert store.append_event.await_args.args[2]["cost_usd"] == .0025
    assert requests == ["/key/list", "/key/generate", "/key/list", "/key/info", "/key/delete"]
    assert KEY not in repr(store.append_event.await_args_list)
