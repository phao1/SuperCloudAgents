from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from sca.agents.provider_errors import model_error_event
from sca.core.errors import AgentTurnError
from sca.core.interfaces import SandboxProvider
from sca.core.models import AgentContext, AgentEvent, ExecExit, Mount, SandboxAdditions, tail

PI_VERSION = "1.5.2"
KEY_ENV = "SCA_MODEL_KEY"


def render_config(base_url: str, model: str) -> str:
    return json.dumps({"providers": {"sca": {
        "baseUrl": base_url.rstrip("/"), "api": "openai-completions",
        "apiKey": "$" + KEY_ENV, "models": [{"id": model}],
    }}})


def _text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(p["text"] for p in content
                     if isinstance(p, dict) and p.get("type") == "text"
                     and isinstance(p.get("text"), str))


def parse_pi_line(line: str, tools: dict[str, dict] | None = None) -> list[AgentEvent]:
    try:
        raw = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, dict):
        return []
    kind = raw.get("type")
    events = []
    if kind == "tool_execution_start" and tools is not None:
        if isinstance(raw.get("toolCallId"), str) and isinstance(raw.get("args"), dict):
            tools[raw["toolCallId"]] = raw["args"]
    elif kind == "tool_execution_end":
        call_id = raw.get("toolCallId")
        args = tools.pop(call_id, {}) if tools is not None and isinstance(call_id, str) else {}
        result = raw.get("result")
        result = result if isinstance(result, dict) else {}
        name = raw.get("toolName")
        data = {"input": args, "output_tail": tail(_text(result.get("content"))),
                "is_error": raw.get("isError", False)}
        if type(raw.get("durationMs")) in (int, float) and raw["durationMs"] >= 0:
            data["duration_ms"] = raw["durationMs"]
        if name == "bash":
            data["command"] = args.get("command", "")
            # Pi's bash result does not report a reliable numeric exit status.
            data["exit_code"] = None
            event = "command"
        elif name in ("write", "edit"):
            details = result.get("details")
            details = details if isinstance(details, dict) else {}
            data["metadata"] = details
            data["changes"] = [{"path": args.get("path", ""), "kind": "update",
                                **({"patch": details["diff"]}
                                   if isinstance(details.get("diff"), str) else {})}]
            event = "file_change"
        else:
            data["name"] = name
            event = "tool"
        events.append(AgentEvent(type=event, data=data))
    elif kind == "message_end":
        message = raw.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return []
        failed = message.get("stopReason") in ("error", "aborted")
        text = _text(message.get("content"))
        if text:
            events.append(AgentEvent(type="message", data={"text": text}))
        usage = message.get("usage")
        if isinstance(usage, dict):
            def number(key: str) -> int:
                value = usage.get(key, 0)
                return value if type(value) is int and value >= 0 else 0
            cost = usage.get("cost")
            total = cost.get("total", 0) if isinstance(cost, dict) else 0
            events.append(AgentEvent(type="usage", data={
                "input_tokens": number("input") + number("cacheRead") + number("cacheWrite"),
                "output_tokens": number("output"), "cached_input_tokens": number("cacheRead"),
                "cost": total if type(total) in (int, float) and 0 <= total < float("inf") else 0,
            }, model_call_succeeded=not failed))
        duration = message.get("durationMs")
        if events and events[-1].type == "usage" and type(duration) in (int, float) and duration >= 0:
            events[-1].data["model_call_duration_ms"] = duration
        if failed:
            events.append(model_error_event(message, "Pi model response failed"))
    return events


# Only the environment contains the credential; neither the config nor argv contains its value.
LAUNCH = '''import json, os, pathlib, sys
config = pathlib.Path(os.environ["PI_CODING_AGENT_DIR"]) / "models.json"
config.parent.mkdir(parents=True, exist_ok=True)
config.write_text(os.environ.pop("SCA_PI_CONFIG"))
binary = os.environ.pop("SCA_PI_BIN", "pi")
os.execvp(binary, ["pi", *sys.argv[1:]])
'''


class PiBackend:
    name = "pi"
    state_transport = "mount"

    def __init__(self, default_model: str, version: str = PI_VERSION,
                 sandbox_image: str = "sca-sandbox-pi:latest", *, executable: str | None = None):
        self.default_model = default_model
        self.version = version
        # pi is installed in its own image; the kernel uses it when the task names no image.
        self.sandbox_image = sandbox_image
        self.executable = executable

    def sandbox_additions(self, ctx: AgentContext) -> SandboxAdditions:
        env = {"HOME": ctx.agent_home}
        if self.state_transport == "sync":
            return SandboxAdditions(env=env)
        home = Path(ctx.agent_home_host)
        home.mkdir(parents=True, exist_ok=True)
        return SandboxAdditions(mounts=[Mount(source=str(home.resolve()), target=ctx.agent_home)],
                                env=env)

    async def run_turn(
        self, sandbox: SandboxProvider, sandbox_id: str, ctx: AgentContext, prompt: str,
        *, session_id: str | None, timeout_s: float,
    ) -> AsyncIterator[AgentEvent]:
        if not ctx.model_base_url or not ctx.model_api_key:
            raise AgentTurnError("Pi requires model_base_url and model_api_key")
        if ctx.agent_version and ctx.agent_version != self.version:
            raise AgentTurnError("Pi version is not installed in the sandbox image")
        sid = session_id or str(uuid4())
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", sid) or sid in {".", ".."}:
            raise AgentTurnError("Invalid Pi session ID")
        model = ctx.model or self.default_model
        argv = ["python3", "-c", LAUNCH, "--print", "--mode", "json", "--offline",
                "--no-approve", "--no-extensions", "--no-skills", "--no-prompt-templates",
                "--no-themes", "--no-mcp", "--provider", "sca", "--model", model,
                "--session-dir", f"{ctx.agent_home}/pi/sessions", "--session-id", sid]
        result = None
        settled, failed, final, stopped = False, False, "", False
        tools: dict[str, dict] = {}
        async for output in sandbox.exec(
            sandbox_id, argv, workdir=ctx.workdir, timeout_s=timeout_s, stdin=prompt.encode(),
            env={"HOME": ctx.agent_home, "PI_CODING_AGENT_DIR": f"{ctx.agent_home}/pi",
                 "PI_OFFLINE": "1", "PI_TELEMETRY": "0", KEY_ENV: ctx.model_api_key,
                 **({"SCA_PI_BIN": self.executable} if self.executable else {}),
                 "SCA_PI_CONFIG": render_config(ctx.model_base_url, model)},
        ):
            if isinstance(output, ExecExit):
                result = output
                continue
            if output.stream != "stdout":
                continue
            # Untrusted output can echo credentials, including inside tool arguments.
            line = output.text.replace(ctx.model_api_key, "[REDACTED]")
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if not isinstance(raw, dict):
                continue
            if raw.get("type") == "session" and not session_id:
                yield AgentEvent(type="session_started", data={"session_id": sid})
            if raw.get("type") == "agent_settled":
                settled = raw.get("aborted") is False
            if raw.get("type") == "message_end":
                message = raw.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    stopped = message.get("stopReason") == "stop"
                    failed = message.get("stopReason") in ("error", "aborted")
            for event in parse_pi_line(line, tools):
                if event.type == "message":
                    final = event.data["text"]
                yield event
        if not result or result.exit_code or result.timed_out or failed or not settled or not stopped:
            raise AgentTurnError("Pi turn failed, timed out, or ended without a completed response")
        yield AgentEvent(type="turn_completed", data={"final_message": final})
