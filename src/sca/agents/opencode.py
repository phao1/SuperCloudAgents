from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from sca.agents.provider_errors import model_error_event
from sca.core.errors import AgentTurnError
from sca.core.interfaces import SandboxProvider, closing_stream
from sca.core.models import AgentContext, AgentEvent, ExecExit, Mount, SandboxAdditions, tail

VERSION_PATTERN = r"2\.3\.0-sca\.(0|[1-9][0-9]*)"


def validate_version(version: str) -> str:
    if not re.fullmatch(VERSION_PATTERN, version):
        raise ValueError("invalid OpenCode version")
    return version


def parse_opencode_line(
    line: str, steps: dict[str, dict[str, int]] | None = None
) -> list[AgentEvent]:
    try:
        raw = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, dict):
        return []
    part = raw.get("part") or {}
    if not isinstance(part, dict):
        return []
    kind = raw.get("type")
    message_id = part.get("messageID")
    step_id = message_id if isinstance(message_id, str) else ""
    timestamp = raw.get("timestamp")
    timestamp = timestamp if type(timestamp) is int and timestamp >= 0 else None
    if kind == "step_start" and steps is not None and timestamp is not None:
        steps[step_id] = {"started_at_ms": timestamp}
    data = {}
    event = None
    if kind == "text":
        event, data = "message", {"text": part.get("text", "")}
    elif kind == "tool_use":
        state = part.get("state") or {}
        if not isinstance(state, dict):
            return []
        name = part.get("tool", "")
        if not isinstance(name, str):
            return []
        inputs = state.get("input") or {}
        if not isinstance(inputs, dict):
            inputs = {}
        metadata = state.get("metadata") or {}
        metadata = metadata.get("metadata", metadata) if isinstance(metadata, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        output = tail(str(state.get("output") or ""))
        if name == "shell":
            event, data = (
                "command",
                {
                    "command": inputs.get("command", ""),
                    "exit_code": metadata.get("exit"),
                    "output_tail": output,
                },
            )
        elif name in {"edit", "write", "patch", "apply_patch"}:
            event, data = (
                "file_change",
                {
                    "changes": metadata.get("files", []),
                    "metadata": metadata,
                    "input": inputs,
                    "output_tail": output,
                },
            )
        else:
            event, data = "tool", {"name": name, "input": inputs, "output_tail": output}
        timing = state.get("time")
        if isinstance(timing, dict):
            start, end = timing.get("start"), timing.get("end")
            if type(start) is int and type(end) is int and 0 <= start <= end:
                data.update(started_at_ms=start, ended_at_ms=end, duration_ms=end - start)
                if steps is not None and step_id in steps:
                    step = steps[step_id]
                    step["first_tool_at_ms"] = min(step.get("first_tool_at_ms", start), start)
    elif kind == "step_finish":
        tokens = part.get("tokens") or {}
        if not isinstance(tokens, dict):
            return []
        cache = tokens.get("cache") or {}
        if not isinstance(cache, dict):
            cache = {}
        event, data = (
            "usage",
            {
                "input_tokens": tokens.get("input", 0),
                "output_tokens": tokens.get("output", 0),
                "reasoning_tokens": tokens.get("reasoning", 0),
                "cached_input_tokens": cache.get("read", 0),
                "cost": part.get("cost", 0),
            },
        )
        step = steps.pop(step_id, {}) if steps is not None else {}
        start = step.get("started_at_ms")
        if timestamp is not None:
            data["ended_at_ms"] = timestamp
        if start is not None and timestamp is not None and timestamp >= start:
            data["started_at_ms"] = start
            data["duration_ms"] = timestamp - start
            # The first tool starts after model generation; later time includes tool execution.
            data["model_call_duration_ms"] = max(
                0, min(timestamp, step.get("first_tool_at_ms", timestamp)) - start
            )
    elif kind == "error":
        error = raw.get("error") or {}
        error_type = error.get("type") if isinstance(error, dict) else None
        if isinstance(error_type, str) and error_type.startswith(("tool.", "permission.")):
            error = None
        return [model_error_event(error, "OpenCode turn reported an error")]
    return [AgentEvent(type=event, data=data, raw=raw, model_call_succeeded=(
        kind == "step_finish" and part.get("reason") not in (None, "error", "aborted")
    ))] if event else []


class OpenCodeBackend:
    name = "opencode"
    state_transport = "mount"

    def __init__(self, versions_dir: Path, default_version: str, default_model: str,
                 *, host_release: bool = False):
        self.versions_dir = versions_dir.resolve()
        self.default_version = validate_version(default_version)
        self.default_model = default_model
        self.host_release = host_release
        self.binary_name = "opencode.exe" if host_release and sys.platform == "win32" else "opencode"
        if not self.versions_dir.is_dir():
            raise FileNotFoundError("OpenCode versions directory does not exist")

    def version_dir(self, version: str | None) -> Path:
        directory = self.versions_dir / validate_version(version or self.default_version)
        if (
            directory.is_symlink()
            or (directory / self.binary_name).is_symlink()
            or not (directory / self.binary_name).is_file()
        ):
            raise AgentTurnError(f"Unknown OpenCode version: {directory.name}")
        return directory

    def _overlay_env(self, ctx: AgentContext) -> dict[str, str]:
        if ctx.agent_overlay is None:
            return {}
        home = Path(ctx.agent_home_host)
        home.mkdir(parents=True, exist_ok=True)
        # Replace atomically: resumed homes are untrusted and may contain symlinks.
        fd, temporary = tempfile.mkstemp(prefix=".overlay-", dir=home)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(ctx.agent_overlay.canonical_json())
                if sys.platform != "win32":
                    os.fchmod(stream.fileno(), 0o444)
            os.replace(temporary, home / "sca-overlay.json")
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {"SCA_AGENT_OVERLAY": f"{ctx.agent_home}/sca-overlay.json"}

    def sandbox_additions(self, ctx: AgentContext) -> SandboxAdditions:
        directory = self.version_dir(ctx.agent_version)
        if self.host_release:
            return SandboxAdditions()
        if self.state_transport != "mount":
            raise AgentTurnError("OpenCode requires mount state transport")
        home = Path(ctx.agent_home_host)
        home.mkdir(parents=True, exist_ok=True)
        return SandboxAdditions(
            mounts=[
                Mount(source=str(directory), target="/opt/opencode", read_only=True),
                Mount(source=str(home.resolve()), target=ctx.agent_home),
            ],
            env={"HOME": ctx.agent_home, **self._overlay_env(ctx)},
        )

    async def run_turn(
        self,
        sandbox: SandboxProvider,
        sandbox_id: str,
        ctx: AgentContext,
        prompt: str,
        *,
        session_id: str | None,
        timeout_s: float,
    ) -> AsyncIterator[AgentEvent]:
        directory = self.version_dir(ctx.agent_version)
        if not (ctx.model_base_url and ctx.model_api_key):
            raise AgentTurnError("OpenCode requires model_base_url and model_api_key")
        model = ctx.model or self.default_model
        config = {
            "providers": {
                "sca": {
                    "name": "SCA gateway",
                    "env": ["SCA_MODEL_KEY"],
                    "package": "@opencode/ai/providers/openai-compatible",
                    "settings": {"baseURL": ctx.model_base_url},
                    "models": {model: {}},
                }
            },
            # Headless runs cannot answer a permission prompt, and a cancelled one fails the turn.
            # Denying the web tools lets the model see a refusal and carry on (sandbox egress is
            # restricted anyway).
            "permission": {"websearch": "deny", "webfetch": "deny"},
        }
        overlay_env = self._overlay_env(ctx)
        if self.host_release and overlay_env:
            config["plugins"] = [{
                "package": str(Path(__file__).with_name("opencode_overlay").resolve()),
                "options": {"path": str(Path(ctx.agent_home_host) / "sca-overlay.json")},
            }]
            overlay_env = {}
        # MCP tool registry
        from sca.tools.runtime import McpAgentContext, redact_mcp_output

        secrets = set()
        if isinstance(ctx, McpAgentContext):
            servers = {}
            for name, server in ctx.mcp_servers.items():
                headers = {key: value.get_secret_value() for key, value in server.headers.items()}
                secrets.update(headers.values())
                for key, value in headers.items():
                    if key.lower() == "authorization":
                        secrets.add(value.partition(" ")[2].strip())
                servers[name] = {"type": "remote", "url": server.url, "headers": headers,
                                 "oauth": False}
            config["mcp"] = {"servers": servers}
        argv = [
            str(directory / self.binary_name) if self.host_release else "/opt/opencode/opencode",
            "run",
            "--standalone",
            "--auto",
            "--archived",
            "--format",
            "json",
            "-m",
            f"sca/{model}",
        ]
        if session_id:
            argv += ["--session", session_id]
        argv += ["--", prompt]
        env = {**overlay_env, "HOME": ctx.agent_home, "SCA_MODEL_KEY": ctx.model_api_key}
        serialized_config = json.dumps(config)
        exec_input = {}
        if secrets and not self.host_release:
            # Docker passes exec environment values on its command line.
            read_config = (
                "import os, sys\n"
                "os.environ['OPENCODE_CONFIG_CONTENT'] = sys.stdin.read()\n"
                "os.execv(sys.argv[1], sys.argv[1:])"
            )
            argv = ["python3", "-c", read_config, *argv]
            exec_input["stdin"] = serialized_config.encode()
        else:
            env["OPENCODE_CONFIG_CONTENT"] = serialized_config
        final, seen_session, finished, failed = "", session_id, False, False
        result = None
        steps: dict[str, dict[str, int]] = {}
        stream = sandbox.exec(
            sandbox_id,
            argv,
            workdir=ctx.workdir,
            env=env,
            timeout_s=timeout_s,
            **exec_input,
        )
        async with closing_stream(stream):
            async for output in stream:
                if isinstance(output, ExecExit):
                    result = output
                    continue
                if output.stream != "stdout":
                    continue
                line = output.text.replace(ctx.model_api_key, "[redacted]")
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if isinstance(raw, dict) and raw.get("sessionID") and not seen_session:
                    seen_session = raw["sessionID"]
                    yield AgentEvent(type="session_started", data={"session_id": seen_session})
                if secrets:
                    line = json.dumps(redact_mcp_output(raw, secrets))
                for event in parse_opencode_line(line, steps):
                    if event.type == "message":
                        final = event.data["text"]
                    if event.type == "error":
                        failed = True
                    if event.type == "usage" and (raw.get("part") or {}).get("reason") == "stop":
                        finished = True
                    yield event
        if not result or result.exit_code or result.timed_out or failed or not finished:
            raise AgentTurnError("OpenCode turn failed or ended without a completed step")
        yield AgentEvent(type="turn_completed", data={"final_message": final})
