from uuid import uuid4

import pytest

from sca.agents.codex import CodexBackend
from sca.config import get_settings
from sca.core.models import AgentContext, Mount, SandboxSpec
from sca.orchestration.worker import make_sandbox_provider

pytestmark = [pytest.mark.integration, pytest.mark.codex]


async def test_real_turn_and_resume(tmp_path):
    settings = get_settings()
    backend = CodexBackend(
        settings.resolved_codex_bin_dir(), settings.codex_auth_file, settings.default_model
    )
    ctx = AgentContext(task_id=uuid4().hex, agent_home_host=str(tmp_path / "home"))
    additions = backend.sandbox_additions(ctx)
    # Sandboxes are network-restricted by default; the provider needs the egress allowlist.
    provider = make_sandbox_provider(settings)
    name = f"test-codex-{uuid4().hex}"
    secret = uuid4().hex
    try:
        sid = await provider.create(
            name,
            SandboxSpec(
                image=settings.sandbox_image,
                mounts=[
                    *additions.mounts,
                    Mount(source=str(tmp_path / "work"), target="/workspace"),
                ],
                env=additions.env,
            ),
        )
        first = [
            e
            async for e in backend.run_turn(
                provider,
                sid,
                ctx,
                f"Create hello.txt containing exactly hello. Remember this secret in conversation "
                f"only, never write it to a file: {secret}. Do not commit. Reply briefly.",
                session_id=None,
                timeout_s=180,
            )
        ]
        assert first[-1].type == "turn_completed"
        assert (tmp_path / "work/hello.txt").read_text().strip() == "hello"
        session = next(e.data["session_id"] for e in first if e.type == "session_started")
        second = [
            e
            async for e in backend.run_turn(
                provider,
                sid,
                ctx,
                "Write the secret I gave you in our previous turn into remembered.txt. "
                "Do not commit. Reply briefly.",
                session_id=session,
                timeout_s=180,
            )
        ]
        assert second[-1].type == "turn_completed"
        assert (tmp_path / "work/remembered.txt").read_text().strip() == secret
    finally:
        await provider.destroy(f"sca-{name}")
