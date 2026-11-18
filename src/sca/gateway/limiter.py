"""Single-process fair model gateway. Run with python -m sca.gateway.limiter."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from sca.config import Settings, get_settings
from sca.core.errors import ScaError


@dataclass
class Bucket:
    rate: float
    capacity: float
    value: float = field(init=False)
    updated: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.value = self.capacity

    def refill(self, now: float) -> None:
        self.value = min(self.capacity, self.value + (now - self.updated) * self.rate / 60)
        self.updated = now


@dataclass
class Ticket:
    tenant: str
    buckets: list[tuple[Bucket, float]]
    future: asyncio.Future[float]
    entered: float = field(default_factory=time.monotonic)


class FairLimiter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.queues: OrderedDict[str, deque[Ticket]] = OrderedDict()
        self.buckets: dict[tuple[str, str, str], Bucket] = {}
        self.registry = CollectorRegistry()
        self.depth = Gauge("sca_gateway_queue_depth", "Waiting calls", registry=self.registry)
        self.wait = Histogram("sca_gateway_queue_wait_seconds", "Admission wait", ["tenant"],
                              registry=self.registry,
                              buckets=(.01, .1, .5, 1, 2, 5, 10, 30, 60, 120))
        self.upstream_429 = Counter("sca_gateway_upstream_429", "Upstream 429s",
                                    registry=self.registry)
        self.rejected = Counter("sca_gateway_rejected", "Queue timeouts", registry=self.registry)

    def reservation(self, model: str, tenant: str, key: str, tokens: int) -> list[tuple[Bucket, float]]:
        s = self.settings
        limits = s.gateway_model_limits.get(model, {})
        levels = [("provider", "all", s.gateway_rpm, s.gateway_tpm),
                  ("model", model, limits.get("rpm", s.gateway_rpm),
                   limits.get("tpm", s.gateway_tpm)),
                  ("tenant", tenant, s.gateway_tenant_rpm, s.gateway_tenant_tpm),
                  ("key", key, s.gateway_key_rpm, s.gateway_key_tpm)]
        result = []
        for level, name, rpm, tpm in levels:
            for unit, rate, capacity, cost in [("rpm", rpm, 1., 1.),
                                                ("tpm", tpm, tpm, float(tokens))]:
                bucket = self.buckets.setdefault((level, name, unit), Bucket(rate, capacity))
                result.append((bucket, cost))
        return result

    async def acquire(self, reservation: list[tuple[Bucket, float]], tenant: str,
                      timeout_s: float | None = None) -> float:
        future = asyncio.get_running_loop().create_future()
        ticket = Ticket(tenant, reservation, future)
        self.queues.setdefault(tenant, deque()).append(ticket)
        self.depth.inc()
        try:
            async with asyncio.timeout(timeout_s if timeout_s is not None else
                                       self.settings.gateway_queue_timeout_s):
                return await future
        except TimeoutError:
            self.rejected.inc()
            raise
        finally:
            queue = self.queues.get(tenant)
            if queue and ticket in queue:
                queue.remove(ticket)
                self.depth.dec()
                if not queue:
                    del self.queues[tenant]

    def dispatch(self) -> None:
        now = time.monotonic()
        # One grant per tenant per round; a blocked tenant cannot block the others.
        for tenant in list(self.queues):
            queue = self.queues[tenant]
            ticket = queue[0]
            for bucket, _ in ticket.buckets:
                bucket.refill(now)
            if not ticket.future.done() and all(b.value >= n for b, n in ticket.buckets):
                for bucket, cost in ticket.buckets:
                    bucket.value -= cost
                queue.popleft()
                self.depth.dec()
                wait = now - ticket.entered
                self.wait.labels(tenant).observe(wait)
                ticket.future.set_result(wait)
                self.queues.move_to_end(tenant)
                if not queue:
                    del self.queues[tenant]

    def correct(self, reservation: list[tuple[Bucket, float]], actual: int) -> None:
        for bucket, estimate in reservation[1::2]:
            bucket.refill(time.monotonic())
            bucket.value = min(bucket.capacity, bucket.value + estimate - actual)

    def pressure(self) -> None:
        now = time.monotonic()
        depth = sum(len(q) for q in self.queues.values())
        oldest = max((now - q[0].entered for q in self.queues.values()), default=0)
        path = self.settings.gateway_pressure_file
        assert isinstance(path, Path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "saturated": depth >= self.settings.gateway_pressure_depth or
                         oldest >= self.settings.gateway_pressure_wait_s,
            "updated_at": datetime.now(UTC).isoformat(),
        }))
        temporary.replace(path)

    async def run(self) -> None:
        last = 0.
        while True:
            self.dispatch()
            if time.monotonic() - last >= 2:
                try:
                    self.pressure()
                except OSError:
                    logging.getLogger(__name__).error("Gateway pressure publication failed")
                last = time.monotonic()
                active = {id(b) for q in self.queues.values() for t in q for b, _ in t.buckets}
                self.buckets = {k: b for k, b in self.buckets.items()
                                if id(b) in active or last - b.updated < 600}
            await asyncio.sleep(.005)


class TenantResolver:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client
        self.cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.lock = asyncio.Lock()

    async def resolve(self, key: str) -> str:
        digest = hashlib.sha256(key.encode()).hexdigest()
        async with self.lock:
            cached = self.cache.get(digest)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            if key in self.settings.gateway_test_keys:
                tenant = self.settings.gateway_test_keys[key]
            else:
                response = await self.client.get(
                    self.settings.model_gateway_url.rstrip("/") + "/key/info",
                    params={"key": key}, headers={
                        "Authorization": f"Bearer {self.settings.model_gateway_master_key}"})
                response.raise_for_status()
                info = response.json()["info"]
                metadata = info.get("metadata") or {}
                tenant = metadata.get("tenant_id")
                if not tenant:
                    match = re.fullmatch(r"task-([0-9a-fA-F-]{36})-turn\d+-attempt\d+(?:-.*)?",
                                         info.get("key_alias", ""))
                    task_id = metadata.get("task_id") or (match[1] if match else None)
                    if not task_id:
                        raise ValueError("key has no task or tenant")
                    from sca.db.engine import get_store

                    tenant = (await get_store().get_task(task_id)).tenant_id
            if not isinstance(tenant, str) or not tenant:
                raise ValueError("invalid tenant")
            self.cache[digest] = (time.monotonic() + 60, tenant)
            self.cache.move_to_end(digest)
            while len(self.cache) > 10000:
                self.cache.popitem(last=False)
            return tenant


class Usage:
    def __init__(self, sse: bool):
        self.sse, self.buffer, self.total = sse, b"", None

    def parse(self, raw: bytes) -> None:
        try:
            usage = json.loads(raw).get("usage") or {}
            total = usage.get("total_tokens")
            if isinstance(total, int) and total >= 0:
                self.total = total
        except (ValueError, AttributeError):
            pass

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        if self.sse:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if line.startswith(b"data:"):
                    self.parse(line[5:].strip())
        if len(self.buffer) > 1048576:
            self.buffer = b""

    def finish(self) -> None:
        if not self.sse:
            self.parse(self.buffer)


HOP = {"host", "content-length", "connection", "transfer-encoding", "keep-alive",
       "proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade"}


def clean_headers(headers: httpx.Headers) -> dict[str, str]:
    excluded = HOP | {x.strip().lower() for x in headers.get("connection", "").split(",")}
    return {k: v for k, v in headers.items() if k.lower() not in excluded}


def create_app(settings: Settings | None = None,
               client: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or get_settings()
    limiter = FairLimiter(settings)
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10),
                                      limits=httpx.Limits(max_connections=400), trust_env=False)
    resolver = TenantResolver(settings, http)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        limiter.pressure()
        task = asyncio.create_task(limiter.run())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if client is None:
                await http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.limiter = limiter

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(limiter.registry), media_type="text/plain")

    @app.post("/v1/{endpoint:path}")
    async def forward(endpoint: str, request: Request) -> Response:
        if endpoint not in {"chat/completions", "completions", "embeddings", "responses"}:
            return JSONResponse({"error": "未知模型端点"}, status_code=404)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                return JSONResponse({"error": "请求过大"}, status_code=413)
        try:
            payload = json.loads(body)
            model = payload["model"]
            if not isinstance(model, str):
                raise TypeError("model must be a string")
            tokens = max(1, int(payload.get("max_completion_tokens",
                                           payload.get("max_tokens", 1024))))
            tokens *= max(1, int(payload.get("n", 1)))
            tokens += len(body) // 4 + 1
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "无效模型请求"}, status_code=400)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or not settings.model_gateway_url:
            return JSONResponse({"error": "缺少网关凭证"}, status_code=401)
        key = auth[7:]
        try:
            tenant = await resolver.resolve(key)
        except (httpx.HTTPError, ScaError, ValueError, KeyError, TypeError, AttributeError):
            return JSONResponse({"error": "无法验证网关凭证"}, status_code=401)
        reservation = limiter.reservation(model, tenant, hashlib.sha256(key.encode()).hexdigest(),
                                          tokens)
        total_wait = 0.
        for attempt in range(4):
            try:
                total_wait += await limiter.acquire(
                    reservation, tenant, settings.gateway_queue_timeout_s - total_wait)
            except TimeoutError:
                return JSONResponse({"error": "网关排队超时"}, status_code=429,
                                    headers={"Retry-After": "1"})
            try:
                upstream = await http.send(http.build_request(
                    "POST", settings.model_gateway_url.rstrip("/") + "/v1/" + endpoint,
                    params=request.query_params, content=bytes(body),
                    headers=clean_headers(httpx.Headers(request.headers))), stream=True)
            except httpx.HTTPError:
                # A lost response may already have been billed. Never replay ambiguous I/O.
                return JSONResponse({"error": "上游连接失败"}, status_code=502)
            if upstream.status_code == 429:
                limiter.upstream_429.inc()
            sse = "text/event-stream" in upstream.headers.get("content-type", "")
            retryable = upstream.status_code == 429 or upstream.status_code >= 500
            # An SSE response can represent an accepted completion, even on a bad status.
            if retryable and not sse and attempt < 3:
                await upstream.aclose()
                await asyncio.sleep(random.uniform(.1, .2) * 2**attempt)
                continue
            break
        usage = Usage(sse)

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    usage.feed(chunk)
                    yield chunk
                usage.finish()
                if usage.total is not None:
                    limiter.correct(reservation, usage.total)
            finally:
                await upstream.aclose()

        headers = clean_headers(upstream.headers)
        headers["x-sca-queue-wait-seconds"] = str(total_wait)
        return StreamingResponse(stream(), status_code=upstream.status_code, headers=headers)

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=get_settings().gateway_limiter_port,
                access_log=False)
