from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sca.agents.opencode import OpenCodeBackend
from sca.config import Settings
from sca.core.models import AgentEvent, AgentEventType
from sca.kernel import activities
from sca.kinds.builtin import load


@pytest.mark.parametrize("pin", [None, "2.3.0-sca.3"])
async def test_default_is_used_by_sandbox_and_turn(tmp_path, monkeypatch, pin):
    load()
    settings = Settings(_env_file=None, data_dir=tmp_path, pip_cache=False,
                        opencode_default_version="2.3.0-sca.1")
    task_id = str(uuid4())
    task = SimpleNamespace(
        kind="coding", result={"sandbox_id": "sandbox"},
        spec={"repo_url": "fixture", "instruction": "task", "agent_version": pin,
              "agent_backend": "opencode"},
    )
    store = SimpleNamespace(
        get_task=AsyncMock(return_value=task),
        resolve_task_opencode_version=AsyncMock(return_value="2.3.0-sca.2"),
        update_result=AsyncMock(), get_session_id=AsyncMock(return_value=None),
        append_event=AsyncMock(),
    )
    sandbox = SimpleNamespace(create=AsyncMock(return_value="sandbox"))
    for version in ("2.3.0-sca.1", "2.3.0-sca.2", "2.3.0-sca.3"):
        path = settings.opencode_versions_dir / version
        path.mkdir(parents=True)
        (path / "opencode").touch()
    backend = OpenCodeBackend(settings.opencode_versions_dir, "2.3.0-sca.1", "model")
    acts = activities.KernelActivities(
        settings=settings, store=store, sandbox=sandbox, agent=backend, objects=SimpleNamespace(),
    )
    acts._ensure_state = AsyncMock(return_value=False)
    acts._before_agent_turn = AsyncMock()
    monkeypatch.setattr(activities.activity, "info", lambda: SimpleNamespace(attempt=1))
    monkeypatch.setattr(activities.activity, "heartbeat", lambda: None)

    expected = pin or "2.3.0-sca.2"

    async def run_turn(sandbox, sandbox_id, ctx, prompt, **kwargs):
        assert ctx.agent_version == expected
        yield AgentEvent(type=AgentEventType.TURN_COMPLETED, data={"final_message": "done"})

    monkeypatch.setattr(backend, "run_turn", run_turn)
    await acts._create_sandbox(task_id)
    sandbox_spec = sandbox.create.call_args.args[1]
    binary_dir = next(mount.source for mount in sandbox_spec.mounts if mount.target == "/opt/opencode")
    assert binary_dir == str(settings.opencode_versions_dir / expected)
    result = await acts._run_agent_turn(activities.TurnInput(task_id, 1, "task"))
    assert result.final_message == "done"
    if pin:
        store.resolve_task_opencode_version.assert_not_called()
    else:
        assert store.resolve_task_opencode_version.await_count == 2
        store.resolve_task_opencode_version.assert_awaited_with(task_id, settings.opencode_default_version)
