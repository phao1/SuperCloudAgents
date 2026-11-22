import asyncio
import itertools
from types import SimpleNamespace

import httpx
import pytest

from sca.gateway.litellm import LiteLLMGateway


@pytest.mark.parametrize("mode", ["grows", "empty", "error", "malformed", "timeout"])
async def test_settle_and_revoke(mode, monkeypatch):
    import sca.gateway.litellm as module

    spends = itertools.chain([0.1, 0.2], itertools.repeat(0.3))
    deleted = []
    polls = []

    async def poll_delay(_):
        if mode == "empty" or (mode == "timeout" and len(polls) == 3):
            raise TimeoutError
        await asyncio.sleep(0)

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(timeout=asyncio.timeout, sleep=poll_delay))

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
            assert (spend, final) == (0.03, False)
        else:
            assert (spend, final) == (None, False)
    finally:
        await gateway.aclose()


@pytest.mark.parametrize("spent,expected,unpriced", [
    (250000, 0.75, False), (0, 1.0, False), (1000000, None, False), (0, None, True),
])
async def test_turn_key_uses_remaining_parent_budget(spent, expected, unpriced, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from sca.config import Settings
    from sca.gateway.model_providers import ProviderInput, ProviderRegistry
    from sca.kinds.coding.activities import CodingActivities, TurnInput
    from sca.kinds.coding.spec import CodingSpec

    async def events(sandbox, sandbox_id, context, *args, **kwargs):
        if unpriced:
            assert context.model_context_window == 8192 and context.model_registered
        if False:
            yield

    if unpriced:
        ProviderRegistry().with_provider(uuid4(), ProviderInput(
            name="Fake local", type="openai-compatible", base_url="http://localhost:11434/v1",
            models=[{"name": "free", "upstream_model": "coder", "context_window": 8192}],
        )).save(tmp_path)
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
        finalize_key=AsyncMock(return_value=(0 if unpriced else 0.01, True)),
    )
    activities = CodingActivities(
        settings=Settings(_env_file=None, data_dir=tmp_path),
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
            model="free" if unpriced else None,
        )
    )
    activities._sandbox_id = AsyncMock(return_value="sandbox")
    activities._sync_fake_home = AsyncMock()
    await activities._run_agent_turn(TurnInput("task", 1, "prompt", None))
    if unpriced:
        assert gateway.issue_key.await_args.kwargs["models"] == ["free"]
        assert gateway.issue_key.await_args.kwargs["max_budget_usd"] is None
        gateway.finalize_key.assert_awaited_once()
        store.add_usage.assert_not_awaited()
        assert store.append_event.await_args.args[2]["cost_usd"] == 0
    elif expected is None:
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


@pytest.mark.parametrize("error", LiteLLMGateway.TRANSIENT_ERRORS)
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_transport_retry(error, method, monkeypatch):
    from sca.gateway import litellm

    async def no_sleep(_):
        pass

    monkeypatch.setattr(litellm.asyncio, "sleep", no_sleep)
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise error("reset", request=request)
        return httpx.Response(200, json={})

    gateway = LiteLLMGateway("http://gateway", "secret", "http://sandbox")
    await gateway._http.aclose()
    gateway._http = httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handle))
    try:
        assert (await gateway._request(method, "/admin")).status_code == 200
        assert calls == 3
    finally:
        await gateway.aclose()


async def test_issue_key_recovers_lost_response(monkeypatch):
    import hashlib
    import json

    from sca.gateway import litellm

    async def no_sleep(_):
        pass

    monkeypatch.setattr(litellm.asyncio, "sleep", no_sleep)
    keys = []
    generated = []

    def handle(request):
        if request.url.path == "/key/list":
            return httpx.Response(200, json={"keys": keys})
        assert request.url.path == "/key/generate"
        body = json.loads(request.content)
        if keys:
            return httpx.Response(400, json={"error": "alias exists"})
        generated.append(body["key"])
        keys.append({"key_alias": body["key_alias"],
                     "token": hashlib.sha256(body["key"].encode()).hexdigest()})
        raise httpx.ReadError("lost response", request=request)

    gateway = LiteLLMGateway("http://gateway", "secret", "http://sandbox")
    await gateway._http.aclose()
    gateway._http = httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handle))
    try:
        first = await gateway.issue_key("alias", models=["model"], ttl_s=60)
        assert await gateway.issue_key("alias", models=["model"], ttl_s=60) == first
        assert generated == [first]
    finally:
        await gateway.aclose()


async def test_transport_retries_exhausted():
    from sca.gateway.litellm import GatewayError

    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("reset", request=request)

    gateway = LiteLLMGateway("http://gateway", "secret", "http://sandbox")
    await gateway._http.aclose()
    gateway._http = httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(GatewayError, match="transport failed"):
            await gateway.list_aliases("prefix")
        assert calls == 4
    finally:
        await gateway.aclose()


async def test_issue_key_replaces_unrecoverable_legacy_key():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path == '/key/list':
            return httpx.Response(200, json={'keys': [{'key_alias': 'alias', 'token': 'old-hash'}]})
        if request.url.path == '/key/delete':
            return httpx.Response(200, json={})
        return httpx.Response(200, json={'key': 'sk-replacement'})

    gateway = LiteLLMGateway('http://gateway', 'secret', 'http://sandbox')
    await gateway._http.aclose()
    gateway._http = httpx.AsyncClient(base_url='http://gateway', transport=httpx.MockTransport(handle))
    try:
        assert await gateway.issue_key('alias', models=['model'], ttl_s=60) == 'sk-replacement'
        assert paths == ['/key/list', '/key/delete', '/key/generate']
    finally:
        await gateway.aclose()
