"""Prometheus metrics and OpenTelemetry integration."""

import asyncio
import time

from opentelemetry import trace
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import text

from sca.core.models import TaskState

_configured = False


def setup_tracing(service_name: str, settings: object) -> None:
    global _configured
    endpoint = getattr(settings, "otlp_endpoint", None)
    if _configured or not endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces"))
    )
    trace.set_tracer_provider(provider)
    _configured = True


def tracing_interceptors() -> list:
    if not _configured:
        return []
    from temporalio.contrib.opentelemetry import TracingInterceptor

    return [TracingInterceptor()]


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        self.requests = Counter(
            "sca_http_requests_total", "HTTP requests", ["route", "status"], registry=self.registry
        )
        self.latency = Histogram(
            "sca_http_request_duration_seconds",
            "HTTP latency",
            ["route", "status"],
            registry=self.registry,
        )
        self.states = Gauge("sca_tasks", "Tasks by state", ["state"], registry=self.registry)
        self.created = Counter("sca_tasks_created_total", "Tasks created", registry=self.registry)
        self.reviews = Counter(
            "sca_reviews_submitted_total", "Reviews submitted", registry=self.registry
        )
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def render(self, store) -> bytes:
        async with self._lock:
            if time.monotonic() >= self._expires:
                async with store._transaction() as session:
                    counts = dict(
                        (
                            await session.execute(text("SELECT * FROM public.sca_task_counts()"))
                        ).all()
                    )
                for state in TaskState:
                    self.states.labels(state=state).set(counts.get(state, 0))
                self._expires = time.monotonic() + 3
        return generate_latest(self.registry)


def instrument_app(app, metrics: Metrics) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)

    @app.middleware("http")
    async def measure(request, call_next):
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = route.path if route is not None else "unmatched"
            metrics.requests.labels(template, str(status)).inc()
            metrics.latency.labels(template, str(status)).observe(time.monotonic() - started)
