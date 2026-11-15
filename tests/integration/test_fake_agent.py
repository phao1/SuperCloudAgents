import json
from uuid import uuid4

import pytest

from sca.agents.fake import FakeBackend
from sca.core.models import AgentContext, Mount, SandboxSpec
from sca.sandbox.docker import DockerSandboxProvider

pytestmark = pytest.mark.integration


async def test_two_turns(tmp_path):
    ctx = AgentContext(task_id=uuid4().hex, agent_home_host=str(tmp_path / "home"))
    backend = FakeBackend()
    additions = backend.sandbox_additions(ctx)
    provider = DockerSandboxProvider()
    name = f"test-fake-{uuid4().hex}"
    try:
        sid = await provider.create(
            name,
            SandboxSpec(
                image="sca-sandbox:latest",
                mounts=[
                    *additions.mounts,
                    Mount(source=str(tmp_path / "work"), target="/workspace"),
                ],
            ),
        )
        prompt = (
            "```sca-fake\necho first > first; exit 7\n```\n```sca-fake\necho second > second\n```"
        )
        first = [
            e
            async for e in backend.run_turn(
                provider, sid, ctx, prompt, session_id=None, timeout_s=10
            )
        ]
        assert [e.type for e in first] == [
            "session_started",
            "command",
            "usage",
            "message",
            "turn_completed",
        ]
        assert first[1].data["exit_code"] == 7
        session = first[0].data["session_id"]
        assert session == f"fake-{ctx.task_id}"
        second = [
            e
            async for e in backend.run_turn(
                provider,
                sid,
                ctx,
                "```sca-fake\necho third > third\n```",
                session_id=session,
                timeout_s=10,
            )
        ]
        assert [e.type for e in second] == ["command", "usage", "message", "turn_completed"]
        assert (tmp_path / "work/first").read_text() == "first\n"
        assert (tmp_path / "work/second").read_text() == "second\n"
        assert not (tmp_path / "work/third").exists()
        assert json.loads((tmp_path / "home/fake_queue.json").read_text())["queue"] == [
            "echo third > third\n"
        ]
        _ = [
            e
            async for e in backend.run_turn(
                provider, sid, ctx, "", session_id=session, timeout_s=10
            )
        ]
        empty = [
            e
            async for e in backend.run_turn(
                provider, sid, ctx, "", session_id=session, timeout_s=10
            )
        ]
        assert [e.type for e in empty] == ["usage", "message", "turn_completed"]
        assert (tmp_path / "work/third").read_text() == "third\n"
    finally:
        await provider.destroy(f"sca-{name}")
