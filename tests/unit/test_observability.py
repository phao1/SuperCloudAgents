from types import SimpleNamespace

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import CollectorRegistry, generate_latest

from sca.observability import Metrics, current_trace_id, setup_tracing, tracing_interceptors


def test_metrics():
    registry = CollectorRegistry()
    metrics = Metrics(registry)
    metrics.created.inc()
    metrics.reviews.inc(2)
    metrics.requests.labels("/v1/tasks/{task_id}", "200").inc()
    metrics.latency.labels("/v1/tasks/{task_id}", "200").observe(0.1)
    output = generate_latest(registry).decode()
    assert "sca_tasks_created_total 1.0" in output
    assert "sca_reviews_submitted_total 2.0" in output
    assert "/v1/tasks/{task_id}" in output


def test_trace_context():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    assert current_trace_id() is None
    with provider.get_tracer("test").start_as_current_span("create") as span:
        assert current_trace_id() == f"{span.get_span_context().trace_id:032x}"
    assert len(exporter.get_finished_spans()) == 1
    provider.shutdown()


def test_disabled_tracing():
    before = trace.get_tracer_provider()
    setup_tracing("test", SimpleNamespace())
    assert trace.get_tracer_provider() is before
    assert tracing_interceptors() == []


def test_tracing_setup(monkeypatch):
    import sca.observability as obs

    installed = []
    monkeypatch.setattr(obs, "_configured", False)
    monkeypatch.setattr(trace, "set_tracer_provider", installed.append)
    setup_tracing("test", SimpleNamespace(otlp_endpoint="http://localhost:4318"))
    setup_tracing("test", SimpleNamespace(otlp_endpoint="http://localhost:4318"))
    assert len(installed) == 1
    assert len(tracing_interceptors()) == 1
    assert installed[0].resource.attributes["service.name"] == "test"
    installed[0].shutdown()
