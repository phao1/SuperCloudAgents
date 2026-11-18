import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from sca.config import Settings
from sca.gateway.limiter import Bucket, FairLimiter, TenantResolver, Usage, create_app


def settings(tmp_path, **kwargs):
    return Settings(_env_file=None, data_dir=tmp_path, model_gateway_url="http://upstream",
                    gateway_test_keys={"test": "tenant"}, **kwargs)


@asynccontextmanager
async def proxy(tmp_path, handler, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        app = create_app(settings(tmp_path, **kwargs), upstream)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://proxy") as client,
        ):
            yield client, app


async def post(client, **kwargs):
    return await client.post("/v1/chat/completions", headers={"Authorization": "Bearer test"},
                             json={"model": "fake", "max_tokens": 16, **kwargs})


def test_bucket_and_correction(tmp_path):
    b = Bucket(60, 1, updated=100)
    b.value = 0
    b.refill(100.5)
    assert b.value == .5
    b.refill(102)
    assert b.value == 1
    limiter = FairLimiter(settings(tmp_path))
    reservation = limiter.reservation("fake", "tenant", "key", 50)
    for bucket, cost in reservation:
        bucket.value -= cost
    limiter.correct(reservation, 10)
    assert reservation[1][0].value == pytest.approx(1000000 - 10, abs=5)
    assert reservation[0][0].value == 0
    other = limiter.reservation("other", "tenant", "key", 10)
    assert other[0][0] is reservation[0][0]
    assert other[4][0] is reservation[4][0]
    assert other[6][0] is reservation[6][0]
    assert other[2][0] is not reservation[2][0]


async def test_round_robin_and_cancellation(tmp_path):
    limiter = FairLimiter(settings(tmp_path))
    tasks = [asyncio.create_task(limiter.acquire([], t)) for t in ["a", "a", "b", "b", "c"]]
    await asyncio.sleep(0)
    limiter.dispatch()
    await asyncio.sleep(0)
    assert [t.done() for t in tasks] == [True, False, True, False, True]
    assert list(limiter.queues) == ["a", "b"]
    limiter.dispatch()
    await asyncio.gather(*tasks)
    waiting = asyncio.create_task(limiter.acquire([(Bucket(1, 0), 1)], "cancel"))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert not limiter.queues
    assert limiter.depth._value.get() == 0


async def test_blocked_tenant_does_not_block_other(tmp_path):
    limiter = FairLimiter(settings(tmp_path))
    blocked = asyncio.create_task(limiter.acquire([(Bucket(1, 0), 1)], "a"))
    ready = asyncio.create_task(limiter.acquire([], "b"))
    await asyncio.sleep(0)
    limiter.dispatch()
    await ready
    assert not blocked.done()
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked


async def test_timeout_429_and_pressure(tmp_path):
    async with proxy(tmp_path, lambda r: httpx.Response(200, json={}), gateway_tpm=1,
                     gateway_queue_timeout_s=.02, gateway_pressure_depth=1) as (client, app):
        request = asyncio.create_task(post(client))
        await asyncio.sleep(.01)
        app.state.limiter.pressure()
        pressure = json.loads((tmp_path / "run/gateway-pressure.json").read_text())
        assert pressure["saturated"] and pressure["updated_at"].endswith("+00:00")
        response = await request
        assert response.status_code == 429
        assert response.headers["retry-after"] == "1"
        app.state.limiter.pressure()
        assert not json.loads((tmp_path / "run/gateway-pressure.json").read_text())["saturated"]
        assert not list((tmp_path / "run").glob("*.tmp"))


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"choices": []}\n\n'
        yield b'data: {"usage":{"total_tokens":10}}\n\n'
        yield b'data: [DONE]\n\n'


async def test_stream_passthrough_and_usage(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())

    async with proxy(tmp_path, handler) as (client, app):
        response = await post(client, stream=True)
        assert response.content == (b'data: {"choices": []}\n\n'
                                    b'data: {"usage":{"total_tokens":10}}\n\n'
                                    b'data: [DONE]\n\n')
        assert seen[0].headers["authorization"] == "Bearer test"
        assert json.loads(seen[0].content)["stream"] is True
        assert app.state.limiter.buckets[("provider", "all", "tpm")].value >= 999990
        assert "x-sca-queue-wait-seconds" in response.headers
        assert (await client.get("/metrics")).status_code == 200


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retries_before_response(tmp_path, status):
    calls = []

    def handler(request):
        calls.append(request.content)
        return httpx.Response(status if len(calls) < 3 else 200, stream=Chunks())

    async with proxy(tmp_path, handler, gateway_rpm=60000, gateway_key_rpm=60000,
                     gateway_tenant_rpm=60000) as (client, app):
        assert (await post(client)).status_code == 200
        assert len(calls) == 3 and len(set(calls)) == 1
        assert app.state.limiter.upstream_429._value.get() == (2 if status == 429 else 0)


async def test_retry_limit(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, stream=Chunks())

    async with proxy(tmp_path, handler, gateway_rpm=60000, gateway_key_rpm=60000,
                     gateway_tenant_rpm=60000) as (client, _):
        assert (await post(client)).status_code == 503
        assert len(calls) == 4


@pytest.mark.parametrize("status", [200, 503])
async def test_sse_never_retried(tmp_path, status):
    calls = []

    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: accepted\n\n"
            raise httpx.ReadError("disconnected")

    def handler(request):
        calls.append(1)
        return httpx.Response(status, headers={"content-type": "text/event-stream"},
                              stream=Broken())

    async with proxy(tmp_path, handler) as (client, _):
        with pytest.raises(httpx.ReadError):
            await post(client)
        assert len(calls) == 1


async def test_ambiguous_transport_not_retried(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadError("lost response")

    async with proxy(tmp_path, handler) as (client, _):
        assert (await post(client)).status_code == 502
        assert len(calls) == 1


async def test_resolver_cache_and_untrusted_tenant(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sca.db import engine

    store = SimpleNamespace(get_task=AsyncMock(return_value=SimpleNamespace(tenant_id="trusted")))
    monkeypatch.setattr(engine, "get_store", lambda: store)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"info": {"key_alias":
            "task-12345678-1234-1234-1234-123456789abc-turn1-attempt1"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolver = TenantResolver(settings(tmp_path), client)
        assert await resolver.resolve("virtual") == "trusted"
        assert await resolver.resolve("virtual") == "trusted"
        assert len(seen) == 1
        store.get_task.assert_awaited_once_with("12345678-1234-1234-1234-123456789abc")
        digest = next(iter(resolver.cache))
        resolver.cache[digest] = (time.monotonic() - 1, "old")
        assert await resolver.resolve("virtual") == "trusted"
        assert len(seen) == 2


def test_usage_and_settings(tmp_path):
    usage = Usage(True)
    usage.feed(b'data: {"usage": {"total_to')
    usage.feed(b'kens":42}}\n\ndata: [DONE]\n\n')
    assert usage.total == 42
    s = settings(tmp_path, gateway_limiter=True)
    assert s.model_gateway_container == "sca-gateway-limiter"
    assert s.model_gateway_port == 4100
    assert s.opencode_brain_gateway_url == "http://127.0.0.1:4100/v1"
    assert settings(tmp_path).model_gateway_port == 4000


async def test_admin_routes_and_invalid_body(tmp_path):
    async with proxy(tmp_path, lambda r: httpx.Response(200)) as (client, _):
        assert (await client.post("/v1/key/generate")).status_code == 404
        assert (await client.post("/key/generate")).status_code == 404
        assert (await client.post("/v1/chat/completions", content="[]")).status_code == 400
        assert (await client.post("/v1/chat/completions", json={"model": "x"})).status_code == 401


async def test_scarce_provider_round_robin(tmp_path):
    limiter = FairLimiter(settings(tmp_path))
    shared = Bucket(1, 1)
    tenants = ["a", "a", "b", "c"]
    tasks = [asyncio.create_task(limiter.acquire([(shared, 1)], tenant)) for tenant in tenants]
    await asyncio.sleep(0)
    order = []
    for _ in tasks:
        shared.value = 1
        limiter.dispatch()
        await asyncio.sleep(0)
        newly_done = [i for i, task in enumerate(tasks) if task.done() and i not in order]
        assert len(newly_done) == 1
        order.extend(newly_done)
    assert [tenants[i] for i in order] == ["a", "b", "c", "a"]
    await asyncio.gather(*tasks)


async def test_worker_peer_uses_limiter_but_admin_stays_litellm(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from sca.orchestration.worker import make_gateway, make_sandbox_provider

    s = settings(tmp_path, gateway_limiter=True, gateway_limiter_port=4123)
    sandbox = SimpleNamespace(ensure_egress=AsyncMock(),
                              address_of=AsyncMock(return_value="172.20.0.5"))
    factory = Mock(return_value=sandbox)
    monkeypatch.setattr("sca.orchestration.worker.DockerSandboxProvider", factory)
    assert make_sandbox_provider(s) is sandbox
    assert factory.call_args.kwargs["peers"] == ["sca-gateway-limiter"]
    gateway = await make_gateway(s, sandbox)
    try:
        assert gateway.sandbox_url == "http://172.20.0.5:4123/v1"
        assert str(gateway._http.base_url) == "http://upstream"
        assert s.opencode_brain_gateway_url == "http://127.0.0.1:4123/v1"
        sandbox.address_of.assert_awaited_once_with("sca-gateway-limiter")
    finally:
        await gateway.aclose()
