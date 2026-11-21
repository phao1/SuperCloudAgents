from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

from sca.agents.provider_errors import model_error_event
from sca.core.errors import AgentTurnError
from sca.core.interfaces import SandboxProvider
from sca.core.models import (
    AgentContext,
    AgentEvent,
    ExecExit,
    Mount,
    SandboxAdditions,
    tail,
)


def parse_codex_line(line: str) -> list[AgentEvent]:
    try:
        raw = json.loads(line)
    except (ValueError, TypeError):
        return []
    if not isinstance(raw, dict):
        return []
    kind = raw.get("type")
    if not isinstance(kind, str):
        return []
    event_type = None
    data = {}
    if kind == "thread.started":
        event_type, data = "session_started", {"session_id": raw.get("thread_id", "")}
    elif kind == "item.completed":
        item = raw.get("item")
        if not isinstance(item, dict):
            return []
        match item.get("type"):
            case "agent_message":
                event_type, data = "message", {"text": item.get("text", "")}
            case "command_execution":
                event_type, data = (
                    "command",
                    {
                        "command": item.get("command", ""),
                        "exit_code": item.get("exit_code"),
                        "output_tail": tail(item.get("aggregated_output") or ""),
                    },
                )
            case "file_change":
                event_type, data = "file_change", {"changes": item.get("changes", [])}
    elif kind == "turn.completed":
        usage = raw.get("usage") or {}
        if not isinstance(usage, dict):
            return []
        event_type, data = (
            "usage",
            {
                key: usage.get(key, 0)
                for key in ("input_tokens", "cached_input_tokens", "output_tokens")
            },
        )
    elif kind in {"turn.failed", "error"}:
        return [model_error_event(raw.get("error") or raw, "Codex turn reported an error")]
    return [AgentEvent(type=event_type, data=data, raw=raw,
                       model_call_succeeded=kind == "turn.completed")] if event_type else []


GATEWAY_KEY_ENV = "SCA_MODEL_KEY"


class CodexBackend:
    name = "codex"
    state_transport = "mount"

    def __init__(self, bin_dir: Path, auth_file: Path | None, default_model: str,
                 *, executable: str = "/opt/codex/codex"):
        """auth_file=None is gateway mode: no credential is mounted, and each turn must
        carry a model_base_url and model_api_key in its context."""
        self.bin_dir = bin_dir.expanduser().resolve()
        self.auth_file = auth_file.expanduser().resolve() if auth_file else None
        self.default_model = default_model
        self.executable = executable

    def sandbox_additions(self, ctx: AgentContext) -> SandboxAdditions:
        home = Path(ctx.agent_home_host)
        home.mkdir(parents=True, exist_ok=True)
        mounts = [
            Mount(source=str(self.bin_dir), target="/opt/codex", read_only=True),
            Mount(source=str(home.resolve()), target=ctx.agent_home),
        ]
        if self.state_transport == "sync":
            if self.auth_file:
                import shutil

                shutil.copyfile(self.auth_file, home / "auth.json")
            return SandboxAdditions(env={"CODEX_HOME": ctx.agent_home})
        if self.auth_file:
            placeholder = home / "auth.json"
            if not placeholder.exists():
                placeholder.touch()
            if placeholder.stat().st_uid != os.getuid():
                raise AgentTurnError("Codex auth placeholder must be owned by the current user")
            mounts.append(Mount(source=str(self.auth_file), target=f"{ctx.agent_home}/auth.json"))
        return SandboxAdditions(mounts=mounts, env={"CODEX_HOME": ctx.agent_home})

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
        argv = [self.executable, "exec"]
        if session_id:
            argv += ["resume", session_id]
        argv += [
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-m",
            ctx.model or self.default_model,
        ]
        if not session_id:
            argv += ["-C", ctx.workdir]
        env = None
        if self.auth_file is None:
            if not (ctx.model_base_url and ctx.model_api_key):
                raise AgentTurnError("gateway mode needs a model_base_url and model_api_key")
            provider = (
                f'{{name="sca",base_url="{ctx.model_base_url}",'
                f'env_key="{GATEWAY_KEY_ENV}",wire_api="responses"}}'
            )
            argv += ["-c", "model_provider=sca", "-c", f"model_providers.sca={provider}"]
            env = {GATEWAY_KEY_ENV: ctx.model_api_key}
        argv += ["-"]
        final_message = ""
        error_message = ""
        stderr = ""
        completed = False
        result = None
        async for output in sandbox.exec(
            sandbox_id,
            argv,
            workdir=ctx.workdir,
            env=env,
            stdin=prompt.encode(),
            timeout_s=timeout_s,
        ):
            if isinstance(output, ExecExit):
                result = output
            elif output.stream == "stderr":
                stderr = tail(stderr + output.text + "\n")
            else:
                line = output.text
                if ctx.model_api_key:
                    line = line.replace(ctx.model_api_key, "[redacted]")
                for event in parse_codex_line(line):
                    if event.type == "message":
                        final_message = event.data["text"]
                    elif event.type == "error":
                        error_message = event.data["message"]
                    elif event.type == "usage":
                        completed = True
                    yield event
        if result is None or result.exit_code != 0 or result.timed_out or not completed:
            diagnostic = model_error_event(stderr, "Codex turn did not complete")
            if diagnostic.data.get("model_provider_error"):
                yield diagnostic
            if diagnostic.data["message"] == "model gateway budget refused":
                error_message = diagnostic.data["message"]
            raise AgentTurnError(
                f"Codex exit_code={result.exit_code if result else None}, "
                f"timed_out={result.timed_out if result else False}, "
                f"turn_completed={completed}: {error_message or 'Codex turn did not complete'}"
            )
        yield AgentEvent(type="turn_completed", data={"final_message": final_message})
