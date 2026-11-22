from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sca.agents.provider_errors import model_error_event
from sca.core.errors import AgentTurnError, SandboxError
from sca.core.interfaces import SandboxProvider, closing_stream
from sca.core.models import AgentContext, AgentEvent, ExecExit, Mount, SandboxAdditions, tail

DSH_VERSION = "0.2.2-rc.5"
KEY_ENV = "DEEPSEEK_API_KEY"
GatewayProtocol = Literal["messages", "openai"]

# The launcher writes a credential-free overlay inside the sandbox, then hands stdin to dsh.
LAUNCH = '''import os, sys, tempfile
path, *argv = sys.argv[1:]
with tempfile.NamedTemporaryFile(dir=os.path.dirname(path), mode="w", encoding="utf-8",
                                 delete=False) as patch:
    patch.write(os.environ.pop("SCA_DSH_PATCH"))
os.replace(patch.name, path)
if os.name == "nt":
    import subprocess
    sys.exit(subprocess.call(argv))
os.execvp(argv[0], argv)
'''


def render_patch(base_url: str, model: str, protocol: GatewayProtocol) -> str:
    disabled = ["tool-web", "web-search-deepseek", "web-deepseek-api", "web-fetch-http",
                "llm-deepseek-account"]
    if protocol == "messages":
        provider = "deepseek-official"
        disabled.append("llm-pi-ai")
        # An explicit base URL in the last overlay overrides prior profile settings.
        adapter = {"id": "llm-deepseek", "config": {"baseURL": base_url, "apiKeyEnv": KEY_ENV}}
    else:
        provider = "sca-gateway"
        disabled.append("llm-deepseek")
        compat: dict[str, str | bool] = {
            "maxTokensField": "max_tokens", "supportsStore": False, "supportsDeveloperRole": False,
        }
        if model.startswith("deepseek-"):
            compat["thinkingFormat"] = "deepseek"
        adapter = {"id": "llm-pi-ai", "config": {"providers": {provider: {
            "baseURL": base_url, "api": "openai-completions", "apiKeyEnv": KEY_ENV,
            # Leave room for the prompt in the limiter's per-key token reservation.
            "defaultMaxTokens": 32_768, "models": [{"id": model}], "compat": compat,
        }}}}
    return json.dumps([
        *({"id": name, "disabled": True} for name in disabled),
        adapter,
        {"id": "agent-default-model", "config": {"provider": provider, "model": model}},
    ])


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _redact(value: Any, key: str) -> Any:
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]")
    if isinstance(value, dict):
        return {_redact(name, key): _redact(item, key) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    return value


def _valid_session(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", value) is not None


def _usage(raw: object) -> AgentEvent | None:
    usage = _object(raw)
    input_tokens, output_tokens = usage.get("inputTokens"), usage.get("outputTokens")
    if type(input_tokens) is not int or type(output_tokens) is not int or min(input_tokens, output_tokens) < 0:
        return None

    def cached(name: str) -> int:
        value = usage.get(name)
        return value if type(value) is int and value >= 0 else 0

    read = cached("cacheReadTokens")
    # Both DSH adapters report uncached input separately from cache reads and writes.
    return AgentEvent(type="usage", data={
        "input_tokens": input_tokens + read + cached("cacheWriteTokens"),
        "cached_input_tokens": read, "output_tokens": output_tokens,
    }, model_call_succeeded=True)


@dataclass
class _RunEvents:
    resume_id: str | None
    session_id: str | None = None
    session_mismatch: bool = False
    tools: dict[str, tuple[str, Any]] = field(default_factory=dict)
    final: dict[str, Any] = field(default_factory=dict)
    last_error: dict[str, Any] = field(default_factory=dict)
    driver_failed: bool = False

    def consume(self, raw: dict[str, Any]) -> AgentEvent | None:
        # @deepseek-ai/dsh-headless src/json-stream.ts defines the projected event stream.
        kind = raw.get("type")
        if kind == "session":
            found = raw.get("sessionId")
            if not _valid_session(found):
                return None
            expected = self.session_id or self.resume_id
            if expected and found != expected:
                self.session_mismatch = True
            elif self.session_id is None:
                self.session_id = found
                if self.resume_id is None:
                    return AgentEvent(type="session_started", data={"session_id": found})
        elif kind == "text":
            if isinstance(text := raw.get("text"), str) and text:
                return AgentEvent(type="message", data={"text": text})
        elif kind == "tool_call":
            call_id, name = raw.get("callId"), raw.get("tool")
            if isinstance(call_id, str) and isinstance(name, str):
                inputs = raw.get("input")
                self.tools[call_id] = (name, inputs)
                return AgentEvent(type="tool", data={"call_id": call_id, "name": name,
                                                     "input": inputs, "status": "started"})
        elif kind == "tool_result":
            return self.tool_result(raw)
        elif kind == "status" and raw.get("phase") == "step_end":
            return _usage(raw.get("usage"))
        elif kind == "final" or (kind == "status" and raw.get("phase") == "turn_end"):
            reason = _object(raw.get("reason"))
            if reason.get("kind") == "error" and (error := _object(reason.get("error"))):
                self.last_error = error
            if kind == "final":
                self.final = raw
        elif kind == "error":
            self.last_error = raw
            self.driver_failed = True
        return None

    def tool_result(self, raw: dict[str, Any]) -> AgentEvent | None:
        call_id, status = raw.get("callId"), raw.get("status")
        if not isinstance(call_id, str) or status not in ("completed", "error"):
            return None
        result = raw.get("result")
        data: dict[str, Any] = {
            "call_id": call_id, "status": status, "is_error": status == "error",
            "output_tail": tail(result) if isinstance(result, str) else "",
        }
        if error := _object(raw.get("error")):
            data["error"] = error
        if (call := self.tools.pop(call_id, None)) is None:
            return AgentEvent(type="tool", data=data)
        name, inputs = call
        data.update(name=name, input=inputs)
        args = _object(inputs)
        if name in ("bash", "pwsh") and isinstance(command := args.get("command"), str):
            # The projected result carries a tool status, with no numeric shell exit code.
            return AgentEvent(type="command", data={**data, "command": command, "exit_code": None})
        if name in ("edit", "write") and status == "completed" and isinstance(path := args.get("file_path"), str):
            return AgentEvent(type="file_change", data={**data, "changes": [{"path": path, "kind": "update"}]})
        return AgentEvent(type="tool", data=data)

    def completion_problem(self) -> str | None:
        if self.driver_failed:
            return "headless runner reported an error"
        if not self.final:
            return "missing final event"
        kind = _object(self.final.get("reason")).get("kind")
        if kind != "completed":
            return "final reason " + (tail(kind, 80) if isinstance(kind, str) else "missing")
        if self.session_mismatch:
            return "session ID mismatch"
        if self.session_id is None:
            return "missing session event"
        if self.final.get("sessionId") != self.session_id:
            return "session ID mismatch"
        if not isinstance(self.final.get("text"), str):
            return "missing final text"
        return None


# DSH can supply a stable LLM code without an HTTP status (for example an in-stream error).
_PROVIDER_CODES = {
    "AUTH": "provider.auth", "QUOTA": "provider.quota", "RATE_LIMIT": "provider.rate-limit",
    "SERVER": "provider.internal", "TIMEOUT": "provider.timeout", "TRANSPORT": "provider.transport",
}


class DshBackend:
    name = "dsh"
    state_transport = "mount"

    def __init__(self, default_model: str, version: str = DSH_VERSION,
                 sandbox_image: str = "sca-sandbox-dsh:latest", *,
                 protocol: GatewayProtocol = "messages", executable: str | None = None,
                 node_executable: str | None = None) -> None:
        self.default_model = default_model
        self.version = version
        self.sandbox_image = sandbox_image if executable is None else None
        self.protocol = protocol
        self.executable = executable
        self.node_executable = node_executable
        if executable is not None:
            self.state_transport = "sync"

    def sandbox_additions(self, ctx: AgentContext) -> SandboxAdditions:
        env = {"HOME": ctx.agent_home, "DSH_HOME": f"{ctx.agent_home}/dsh"}
        if self.state_transport == "sync":
            return SandboxAdditions(env=env)
        home = Path(ctx.agent_home_host)
        home.mkdir(parents=True, exist_ok=True)
        return SandboxAdditions(mounts=[Mount(source=str(home.resolve()), target=ctx.agent_home)], env=env)

    async def run_turn(
        self, sandbox: SandboxProvider, sandbox_id: str, ctx: AgentContext, prompt: str,
        *, session_id: str | None, timeout_s: float,
    ) -> AsyncIterator[AgentEvent]:
        key = ctx.model_api_key
        if not ctx.model_base_url or not key:
            raise AgentTurnError("DSH requires model_base_url and model_api_key")
        if ctx.agent_version and ctx.agent_version != self.version:
            scope = "on the host" if self.executable else "in the sandbox image"
            raise AgentTurnError(f"DSH version is not installed {scope}")
        if session_id is not None and (not _valid_session(session_id) or key in session_id):
            raise AgentTurnError("Invalid DSH session ID")
        base_url = ctx.model_base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        patch = f"{ctx.agent_home}/sca-dsh.patch.yml"
        command = [self.executable or "dsh"]
        if self.node_executable is not None:
            command.insert(0, self.node_executable)
        argv = ["python3", "-c", LAUNCH, patch, *command, "--profile", "headless", "--patch", patch,
                "--json", "--permission-mode", "workspace-write", "--approval-policy", "never"]
        if session_id is not None:
            argv.extend(["--session-id", session_id])
        argv.append("-")
        env = {"HOME": ctx.agent_home, "DSH_HOME": f"{ctx.agent_home}/dsh",
               "DSH_PERMISSION_MODE": "workspace-write", "DSH_TELEMETRY_MODE": "DISABLED",
               KEY_ENV: key, "DEEPSEEK_BASE_URL": base_url,
               "SCA_DSH_PATCH": render_patch(base_url, ctx.model or self.default_model, self.protocol)}

        run = _RunEvents(session_id)
        result = None
        stderr = ""
        try:
            stream = sandbox.exec(sandbox_id, argv, workdir=ctx.workdir, env=env,
                                  stdin=prompt.encode(), timeout_s=timeout_s)
            async with closing_stream(stream):
                async for output in stream:
                    if isinstance(output, ExecExit):
                        result = output
                        continue
                    if output.stream == "stderr":
                        stderr = tail(f"{stderr}\n{_redact(output.text, key)}".lstrip("\n"))
                        continue
                    try:
                        raw = _redact(_object(json.loads(output.text)), key)
                    except (ValueError, RecursionError):
                        continue
                    if event := run.consume(raw):
                        yield event
        except SandboxError as exc:
            result = None
            stderr = tail(f"{stderr}\n{_redact(str(exc), key)}".lstrip("\n"))

        problem = run.completion_problem()
        if result is not None and result.exit_code == 0 and not result.timed_out and problem is None:
            yield AgentEvent(type="turn_completed", data={"final_message": run.final["text"]})
            return

        exit_status = f"exit code {result.exit_code}" if result is not None else "missing exit status"
        details = [f"DSH turn failed ({exit_status})"]
        if result is not None and result.timed_out:
            details.append("timed out")
        if problem:
            details.append(problem)
        if run.last_error:
            details.append("last error event " + tail(json.dumps(run.last_error, ensure_ascii=False)))
        if stderr:
            details.append("stderr tail " + stderr)
        diagnosis = " | ".join(details)
        code = run.last_error.get("code")
        provider_hint = _PROVIDER_CODES.get(code) if isinstance(code, str) else None
        error = model_error_event({"error": run.last_error, "name": provider_hint, "detail": stderr}, diagnosis)
        if error.data["message"] != diagnosis:
            yield error
            diagnosis += " | " + error.data["message"]
        raise AgentTurnError(diagnosis)
