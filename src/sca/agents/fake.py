from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path

from sca.agents.live import activity_start
from sca.core.errors import AgentTurnError
from sca.core.interfaces import SandboxProvider, closing_stream
from sca.core.models import (
    AgentContext,
    AgentEvent,
    ExecExit,
    Mount,
    SandboxAdditions,
    tail,
)


class FakeBackend:
    name = "fake"
    state_transport = "mount"
    uses_model_gateway = False

    def sandbox_additions(self, ctx: AgentContext) -> SandboxAdditions:
        home = Path(ctx.agent_home_host)
        home.mkdir(parents=True, exist_ok=True)
        if self.state_transport == "sync":
            return SandboxAdditions()
        return SandboxAdditions(mounts=[Mount(source=str(home.resolve()), target=ctx.agent_home)])

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
        queue_path = Path(ctx.agent_home_host) / "fake_queue.json"
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        # The script being run stays in "current" until the turn completes, so a retried turn
        # (worker crash, activity retry) runs the same script again instead of the next one.
        state = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else {}
        if isinstance(state, list):
            state = {"queue": state}
        queue = state.get("queue", [])
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        if state.get("current") is not None and state.get("prompt") == digest:
            script = state["current"]
        else:
            queue.extend(
                re.findall(
                    r"^```sca-fake[^\S\n]*\n(.*?)^```[^\S\n]*$",
                    prompt,
                    flags=re.MULTILINE | re.DOTALL,
                )
            )
            script = queue.pop(0) if queue else None
        queue_path.write_text(json.dumps({"queue": queue, "current": script, "prompt": digest}),
                              encoding="utf-8")
        if session_id is None:
            yield AgentEvent(type="session_started", data={"session_id": f"fake-{ctx.task_id}"})
        if script is not None:
            yield activity_start("command", script, "fake-script")
            started = time.monotonic()
            output = ""
            result = None
            stream = sandbox.exec(
                sandbox_id, ["bash", "-lc", script], workdir=ctx.workdir, timeout_s=timeout_s
            )
            async with closing_stream(stream):
                async for event in stream:
                    if isinstance(event, ExecExit):
                        result = event
                    else:
                        output = tail(output + event.text + "\n")
            if result is None or result.timed_out:
                raise AgentTurnError("Fake script timed out or did not exit")
            yield AgentEvent(
                type="command",
                data={"command": script, "exit_code": result.exit_code, "output_tail": output,
                      "call_id": "fake-script", "duration_s": time.monotonic() - started},
            )
        queue_path.write_text(json.dumps({"queue": queue, "current": None}), encoding="utf-8")
        yield AgentEvent(
            type="usage", data={"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}
        )
        message = "Fake turn completed."
        yield AgentEvent(type="message", data={"text": message})
        yield AgentEvent(type="turn_completed", data={"final_message": message})
