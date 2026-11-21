import json
from pathlib import Path

import pytest

from sca.agents.codex import CodexBackend, parse_codex_line
from sca.core.errors import AgentTurnError
from sca.core.models import AgentContext, ExecExit, ExecOutput, tail


def test_fixture():
    lines = (Path(__file__).parent / "fixtures/codex_turn.jsonl").read_text().splitlines()
    events = [event for line in lines for event in parse_codex_line(line)]
    assert [event.type for event in events] == [
        "session_started",
        "message",
        "command",
        "command",
        "file_change",
        "command",
        "message",
        "usage",
    ]
    assert events[0].data["session_id"] == "01a1d9d6-f916-7b90-b8f9-6ba787195501"
    assert events[2].data["output_tail"] == "/workspace\ncalc.py\n"
    assert events[4].data == {"changes": [{"path": "/workspace/calc.py", "kind": "update"}]}
    assert events[-1].data == {
        "input_tokens": 58559,
        "cached_input_tokens": 53120,
        "output_tokens": 221,
    }
    assert all(event.raw for event in events)


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[]",
        "null",
        "42",
        "{}",
        '{"type":"item.started"}',
        '{"type":"item.completed","item":null}',
    ],
)
def test_ignored(line):
    assert parse_codex_line(line) == []


def test_errors_and_tail():
    for raw in [
        {"type": "turn.failed", "error": {"message": "failed"}},
        {"type": "error", "message": "failed"},
    ]:
        (event,) = parse_codex_line(json.dumps(raw))
        assert event.data == {"message": "Codex turn reported an error"}
        assert event.raw is None
    output = "中" * 5000
    (event,) = parse_codex_line(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "aggregated_output": output,
                    "exit_code": 3,
                    "command": "cmd",
                },
            }
        )
    )
    assert event.data == {"command": "cmd", "exit_code": 3, "output_tail": tail(output)}


@pytest.mark.parametrize(
    "exit_code,timed_out,complete", [(3, False, True), (124, True, False), (0, False, False)]
)
async def test_abnormal_turn(tmp_path, exit_code, timed_out, complete):
    class Sandbox:
        async def exec(self, *args, **kwargs):
            yield ExecOutput(stream="stderr", text="diagnostic")
            yield ExecOutput(stream="stdout", text='{"type":"error","message":"failure"}')
            if complete:
                yield ExecOutput(stream="stdout", text='{"type":"turn.completed","usage":{}}')
            yield ExecExit(exit_code=exit_code, timed_out=timed_out)

    backend = CodexBackend(tmp_path, tmp_path / "auth", "model")
    ctx = AgentContext(task_id="test", agent_home_host=str(tmp_path))
    with pytest.raises(AgentTurnError, match="Codex turn reported an error") as exc:
        _ = [
            event
            async for event in backend.run_turn(
                Sandbox(), "id", ctx, "prompt", session_id=None, timeout_s=10
            )
        ]
    assert "diagnostic" not in str(exc.value)


async def test_success_and_additions(tmp_path):
    from sca.core.models import Mount

    bin_dir = tmp_path / "bin"
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    ctx = AgentContext(task_id="test", agent_home_host=str(tmp_path / "home"), model="override")
    backend = CodexBackend(bin_dir, auth, "default")
    additions = backend.sandbox_additions(ctx)
    assert additions.env == {"CODEX_HOME": "/agent_home"}
    assert additions.mounts[-1] == Mount(source=str(auth), target="/agent_home/auth.json")
    assert (tmp_path / "home/auth.json").read_text() == ""
    assert additions.mounts[0].read_only

    class Sandbox:
        async def exec(self, sandbox_id, argv, **kwargs):
            assert argv[-1] == "-"
            assert argv[argv.index("-m") + 1] == "override"
            assert kwargs["workdir"] == "/workspace"
            assert kwargs["stdin"] == b"prompt"
            if "resume" in argv:
                assert "-C" not in argv
                assert argv[2:4] == ["resume", "session"]
            else:
                assert argv[argv.index("-C") + 1] == "/workspace"
            for raw in [
                {"type": "thread.started", "thread_id": "session"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "last"}},
                {"type": "turn.completed", "usage": {}},
            ]:
                yield ExecOutput(stream="stdout", text=json.dumps(raw))
            yield ExecExit(exit_code=0)

    for session in (None, "session"):
        events = [
            e
            async for e in backend.run_turn(
                Sandbox(), "sandbox", ctx, "prompt", session_id=session, timeout_s=10
            )
        ]
        assert events[-1].type == "turn_completed"
        assert events[-1].data == {"final_message": "last"}
