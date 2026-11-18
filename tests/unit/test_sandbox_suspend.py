import asyncio
from unittest.mock import AsyncMock

import pytest

from sca.core.errors import SandboxError
from sca.core.interfaces import pause_sandbox, unpause_sandbox
from sca.core.models import ExecExit, ExecOutput
from sca.sandbox.docker import DockerSandboxProvider


async def test_optional_capability():
    await pause_sandbox(object(), "sandbox")
    await unpause_sandbox(object(), "sandbox")


async def test_idempotent_freezer():
    provider = DockerSandboxProvider()
    provider._cli = AsyncMock(side_effect=[(0, "", ""), (1, "", "already paused"),
                                          (0, "", ""), (1, "", "is not paused")])
    await provider.pause("one")
    await provider.pause("one")
    await provider.unpause("one")
    await provider.unpause("one")
    provider._cli = AsyncMock(return_value=(1, "", "permission denied"))
    with pytest.raises(SandboxError):
        await provider.pause("one")


async def test_exec_serializes_pause(monkeypatch):
    provider = DockerSandboxProvider()
    provider._suspend_enabled = True
    calls = []
    provider._cli = AsyncMock(side_effect=lambda *a, **kw: (calls.append(a[0]) or (0, "", "")))
    entered, finish = asyncio.Event(), asyncio.Event()

    async def stream(*args, **kwargs):
        calls.append("exec")
        entered.set()
        await finish.wait()
        yield ExecExit(exit_code=0)

    monkeypatch.setattr("sca.sandbox.docker.stream_exec", stream)

    async def run():
        return [event async for event in provider.exec("one", ["true"])]

    execution = asyncio.create_task(run())
    await entered.wait()
    pause = asyncio.create_task(provider.pause("one"))
    await asyncio.sleep(0)
    assert calls == ["unpause", "exec"]
    finish.set()
    await execution
    await pause
    assert calls == ["unpause", "exec", "pause"]


async def test_exec_retries_external_pause_once(monkeypatch):
    provider = DockerSandboxProvider()
    provider._suspend_enabled = True
    provider._cli = AsyncMock(return_value=(0, "", ""))
    attempts = 0

    async def stream(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield ExecOutput(stream="stderr", text="Error response from daemon: Container one is paused")
            yield ExecExit(exit_code=1)
        else:
            yield ExecExit(exit_code=0)

    monkeypatch.setattr("sca.sandbox.docker.stream_exec", stream)
    events = [event async for event in provider.exec("one", ["true"])]
    assert events == [ExecExit(exit_code=0)]
    assert attempts == 2
    assert provider._cli.await_count == 2


async def test_destroy_paused_does_not_require_resume():
    provider = DockerSandboxProvider()
    provider._cli = AsyncMock(return_value=(0, "", ""))
    await provider.pause("one")
    await provider.destroy("one")
    assert provider._cli.call_args.args == ("rm", "-f", "one")


async def test_kernel_usage_resumes_before_transport():
    from types import SimpleNamespace

    from sca.kernel.activities import KernelActivities

    kernel = object.__new__(KernelActivities)
    kernel.store = SimpleNamespace(get_task=AsyncMock(return_value=SimpleNamespace(
        result={"sandbox_id": "one"})))
    kernel.sandbox = SimpleNamespace(unpause=AsyncMock())
    assert await kernel._sandbox_id("task") == "one"
    kernel.sandbox.unpause.assert_awaited_once_with("one")


async def test_activity_accounting_across_releases_and_retry(monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from sca.kernel.activities import KernelActivities

    kernel = object.__new__(KernelActivities)
    result = {"sandbox_opened_at": 100.0}
    events = [SimpleNamespace(type="agent.usage", created_at=datetime.fromtimestamp(105, UTC),
                              payload={"sandbox_suspended_s": 6.0})]

    async def append(task_id, event_type, payload, **kw):
        if not any(e.type == event_type and e.payload.get("segment") == payload.get("segment")
                   for e in events):
            events.append(SimpleNamespace(type=event_type, payload=payload))

    async def update(task_id, payload):
        result.update(payload)

    kernel.store = SimpleNamespace(
        get_task=AsyncMock(side_effect=lambda _: SimpleNamespace(kind="coding", result=result)),
        list_events=AsyncMock(side_effect=lambda *a, **kw: list(events)),
        append_event=AsyncMock(side_effect=append), update_result=AsyncMock(side_effect=update))
    monkeypatch.setattr("sca.kernel.activities.time.time", lambda: 110.0)
    await kernel._record_sandbox_activity("task")
    assert result["sandbox_alive_s"] == 10
    assert result["sandbox_active_s"] == 4
    assert result["sandbox_activity_ratio"] == .4
    await kernel._record_sandbox_activity("task")
    assert kernel.store.append_event.await_count == 1
    result["sandbox_opened_at"] = 120.0
    monkeypatch.setattr("sca.kernel.activities.time.time", lambda: 130.0)
    await kernel._record_sandbox_activity("task")
    assert result["sandbox_alive_s"] == 20
    assert result["sandbox_active_s"] == 14


async def test_tool_output_does_not_trigger_retry(monkeypatch):
    provider = DockerSandboxProvider()
    provider._cli = AsyncMock(return_value=(0, "", ""))
    attempts = 0

    async def stream(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        yield ExecOutput(stream="stdout", text="the container is paused")
        yield ExecExit(exit_code=0)

    monkeypatch.setattr("sca.sandbox.docker.stream_exec", stream)
    assert len([e async for e in provider.exec("one", ["echo"])]) == 2
    assert attempts == 1


async def test_activity_uses_persisted_transitions_on_interruption(monkeypatch):
    from types import SimpleNamespace

    from sca.kernel.activities import KernelActivities

    kernel = object.__new__(KernelActivities)
    result = {"sandbox_opened_at": 100.0}
    kernel.store = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(kind="coding", result=result)),
        list_events=AsyncMock(return_value=[
            SimpleNamespace(type="sandbox.transition", payload={"paused": True, "at": 102.0}),
            SimpleNamespace(type="sandbox.transition", payload={"paused": False, "at": 105.0}),
            SimpleNamespace(type="sandbox.transition", payload={"paused": True, "at": 108.0}),
        ]), append_event=AsyncMock(), update_result=AsyncMock())
    monkeypatch.setattr("sca.kernel.activities.time.time", lambda: 110.0)
    await kernel._record_sandbox_activity("task")
    recorded = kernel.store.append_event.call_args.args[2]
    assert recorded["sandbox_alive_s"] == 10
    assert recorded["sandbox_active_s"] == 5
    assert recorded["sandbox_activity_ratio"] == .5


async def test_concurrent_execs_share_the_sandbox(monkeypatch):
    # Regression: the pause lock once serialised every command, so parallel checks ran in turn.
    provider = DockerSandboxProvider()
    provider._handd_enabled = False
    provider._cli = AsyncMock(return_value=(0, "", ""))
    running, release = 0, asyncio.Event()
    both = asyncio.Event()

    async def stream(*args, **kwargs):
        nonlocal running
        running += 1
        if running == 2:
            both.set()
        await release.wait()
        yield ExecExit(exit_code=0)

    monkeypatch.setattr("sca.sandbox.docker.stream_exec", stream)

    async def run():
        return [event async for event in provider.exec("one", ["true"])]

    tasks = [asyncio.create_task(run()) for _ in range(2)]
    await asyncio.wait_for(both.wait(), 1)
    release.set()
    await asyncio.gather(*tasks)
    provider._cli.assert_not_awaited()  # no unpause when suspension is off
