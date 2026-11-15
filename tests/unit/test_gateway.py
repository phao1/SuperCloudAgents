import itertools

import httpx
import pytest

from sca.gateway.litellm import LiteLLMGateway


@pytest.mark.parametrize("mode", ["grows", "empty", "error", "malformed", "timeout"])
async def test_settle_and_revoke(mode, monkeypatch):
    import sca.gateway.litellm as module

    original_timeout = module.asyncio.timeout
    monkeypatch.setattr(module.asyncio, "timeout", lambda _: original_timeout(0.04))
    original_sleep = module.asyncio.sleep
    monkeypatch.setattr(module.asyncio, "sleep", lambda _: original_sleep(0.001))
    spends = itertools.chain([0.1, 0.2], itertools.repeat(0.3))
    deleted = []
    polls = []

    def handle(request):
        if request.url.path == "/key/delete":
            deleted.append(True)
            return httpx.Response(200, json={})
        if mode == "error":
            return httpx.Response(503)
        if mode == "malformed":
            return httpx.Response(200, text="bad json")
        if request.url.path == "/key/list":
            return httpx.Response(
                200, json={"keys": [] if mode == "empty" else [{"token": "hashed"}]}
            )
        polls.append(True)
        spend = len(polls) / 100 if mode == "timeout" else next(spends)
        return httpx.Response(200, json={"info": {"spend": spend}})

    gateway = LiteLLMGateway("http://gateway", "secret", "http://sandbox")
    await gateway._http.aclose()
    gateway._http = httpx.AsyncClient(
        base_url="http://gateway", transport=httpx.MockTransport(handle)
    )
    try:
        spend, final = await gateway.finalize_key("alias")
        assert deleted == [True]
        if mode == "grows":
            assert (spend, final) == (0.3, True)
            # two changes, then the value has to hold for SETTLE_POLLS more polls
            assert len(polls) == 3 + LiteLLMGateway.SETTLE_POLLS
        elif mode == "timeout":
            assert spend > 0 and not final
        else:
            assert (spend, final) == (None, False)
    finally:
        await gateway.aclose()


@pytest.mark.parametrize("spent,expected", [(250000, 0.75), (0, 1.0), (1000000, None)])
async def test_turn_key_uses_remaining_parent_budget(spent, expected, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sca.config import Settings
    from sca.kinds.coding.activities import CodingActivities, TurnInput
    from sca.kinds.coding.spec import CodingSpec

    async def events(*args, **kwargs):
        if False:
            yield

    monkeypatch.setattr(
        "sca.kinds.coding.activities.activity.info", lambda: SimpleNamespace(attempt=1)
    )
    store = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(usage={"cost_micro_usd": spent}, result={})),
        get_session_id=AsyncMock(return_value=None),
        append_event=AsyncMock(),
        add_usage=AsyncMock(),
    )
    gateway = SimpleNamespace(
        sandbox_url="http://gateway",
        issue_key=AsyncMock(return_value="ephemeral"),
        finalize_key=AsyncMock(return_value=(0.01, True)),
    )
    activities = CodingActivities(
        settings=Settings(data_dir=tmp_path),
        store=store,
        sandbox=SimpleNamespace(),
        agent=SimpleNamespace(name="fake", run_turn=events),
        objects=SimpleNamespace(),
        gateway=gateway,
    )
    activities._ensure_state = AsyncMock(return_value=False)
    activities._spec = AsyncMock(
        return_value=CodingSpec(
            repo_url="x",
            instruction="x",
            max_cost_usd=1,
        )
    )
    activities._sandbox_id = AsyncMock(return_value="sandbox")
    activities._sync_fake_home = AsyncMock()
    await activities._run_agent_turn(TurnInput("task", 1, "prompt", None))
    if expected is None:
        gateway.issue_key.assert_not_awaited()
        gateway.finalize_key.assert_not_awaited()
        store.append_event.assert_not_awaited()
    else:
        assert gateway.issue_key.await_args.kwargs["max_budget_usd"] == expected
        store.add_usage.assert_awaited_once_with("task", {"cost_micro_usd": 10000})
        payload = store.append_event.await_args.args[2]
        assert payload["cost_final"] is True


async def test_gateway_budget_failure_is_non_retryable(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from temporalio.exceptions import ApplicationError

    from sca.config import Settings
    from sca.core.errors import AgentTurnError
    from sca.kinds.coding.activities import CodingActivities, TurnInput
    from sca.kinds.coding.spec import CodingSpec

    async def events(*args, **kwargs):
        raise AgentTurnError("model budget exceeded; exit=1")
        yield

    monkeypatch.setattr(
        "sca.kinds.coding.activities.activity.info", lambda: SimpleNamespace(attempt=1)
    )
    store = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(usage={}, result={})),
        get_session_id=AsyncMock(return_value=None),
        append_event=AsyncMock(),
        add_usage=AsyncMock(),
        check_budget=AsyncMock(return_value="Task cost budget exhausted"),
    )
    gateway = SimpleNamespace(
        sandbox_url="http://gateway",
        issue_key=AsyncMock(return_value="ephemeral"),
        finalize_key=AsyncMock(return_value=(0.01, True)),
    )
    activities = CodingActivities(
        settings=Settings(data_dir=tmp_path),
        store=store,
        sandbox=SimpleNamespace(),
        agent=SimpleNamespace(name="fake", run_turn=events),
        objects=SimpleNamespace(),
        gateway=gateway,
    )
    activities._ensure_state = AsyncMock(return_value=False)
    activities._spec = AsyncMock(return_value=CodingSpec(repo_url="x", instruction="x"))
    activities._sandbox_id = AsyncMock(return_value="sandbox")
    with pytest.raises(ApplicationError, match="budget") as exc:
        await activities._run_agent_turn(TurnInput("task", 1, "prompt", None))
    assert exc.value.non_retryable
    assert exc.value.type == "BudgetExceeded"
    gateway.finalize_key.assert_awaited_once()
    store.add_usage.assert_awaited_once_with("task", {"cost_micro_usd": 10000})


async def test_deleting_a_key_that_is_not_there_is_success():
    """LiteLLM answers 404 'No keys found' for an unknown alias; revocation is idempotent."""
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/key/delete":
            return httpx.Response(404, json={"error": {"message": "{'error': 'No keys found'}"}})
        if request.url.path == "/key/generate":
            return httpx.Response(200, json={"key": "sk-new"})
        return httpx.Response(200, json={"keys": []})

    gateway = LiteLLMGateway("http://gateway", "secret", "http://sandbox")
    await gateway._http.aclose()
    gateway._http = httpx.AsyncClient(
        base_url="http://gateway", transport=httpx.MockTransport(handle)
    )
    try:
        assert await gateway.issue_key("a", models=["m"], ttl_s=60) == "sk-new"
        other = httpx.Response(404, json={"error": {"message": "route not found"}})
        gateway._http = httpx.AsyncClient(
            base_url="http://gateway", transport=httpx.MockTransport(lambda r: other)
        )
        from sca.gateway.litellm import GatewayError

        with pytest.raises(GatewayError):  # any other 404 is still an error
            await gateway._post("/key/delete", {"key_aliases": ["a"]})
    finally:
        await gateway.aclose()
