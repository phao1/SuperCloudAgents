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


def test_business_metrics_labels():
    from sca.observability import BusinessMetrics

    registry = CollectorRegistry()
    metrics = BusinessMetrics(registry)
    metrics.duration.labels("coding", "SUCCEEDED").observe(12)
    metrics.turns.labels("coding").observe(2)
    metrics.tokens.labels("coding", "cached").inc(9)
    metrics.cost.labels("coding").inc(0.2)
    metrics.verifications.labels("coding", "true").inc()
    metrics.sandbox_create.labels("docker").observe(0.5)
    assert (
        registry.get_sample_value(
            "sca_task_duration_seconds_count", {"kind": "coding", "final_state": "SUCCEEDED"}
        )
        == 1
    )
    assert registry.get_sample_value("sca_task_turns_sum", {"kind": "coding"}) == 2
    assert (
        registry.get_sample_value("sca_tokens_total", {"kind": "coding", "direction": "cached"})
        == 9
    )
    assert registry.get_sample_value("sca_cost_usd_total", {"kind": "coding"}) == 0.2
    assert (
        registry.get_sample_value("sca_verifications_total", {"kind": "coding", "passed": "true"})
        == 1
    )
    assert registry.get_sample_value("sca_sandbox_create_seconds_count", {"driver": "docker"}) == 1


async def test_metric_failure_does_not_fail_activity(monkeypatch):
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, Mock

    from sca.kinds.coding.activities import CodingActivities, SetStateInput
    from sca.observability import business_metrics

    task = SimpleNamespace(kind="coding", state="SUCCEEDED", created_at=datetime.now(UTC))
    activities = object.__new__(CodingActivities)
    activities.store = SimpleNamespace(
        get_task=AsyncMock(return_value=SimpleNamespace(state="RUNNING")),
        set_state=AsyncMock(return_value=task),
    )
    monkeypatch.setattr(
        business_metrics.duration, "labels", Mock(side_effect=RuntimeError("broken"))
    )
    await activities.set_state(SetStateInput("id", "SUCCEEDED"))
    activities.store.set_state.assert_awaited_once()


async def test_terminal_activity_records_once(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    import sca.kernel.activities as module
    from sca.observability import BusinessMetrics

    registry = CollectorRegistry()
    monkeypatch.setattr(module, "business_metrics", BusinessMetrics(registry))
    terminal = SimpleNamespace(
        kind="coding",
        state="FAILED",
        result={},
        created_at=datetime.now(UTC) - timedelta(seconds=20),
    )
    activities = object.__new__(module.KernelActivities)
    activities.store = SimpleNamespace(
        get_task=AsyncMock(side_effect=[SimpleNamespace(state="RUNNING"), terminal]),
        set_state=AsyncMock(return_value=terminal),
        list_events=AsyncMock(return_value=[SimpleNamespace(type="agent.turn_completed")]),
    )
    await activities.set_state(module.SetStateInput("id", "FAILED"))
    await activities.set_state(module.SetStateInput("id", "FAILED"))
    assert (
        registry.get_sample_value(
            "sca_task_duration_seconds_count", {"kind": "coding", "final_state": "FAILED"}
        )
        == 1
    )
    assert registry.get_sample_value("sca_task_turns_sum", {"kind": "coding"}) == 1


async def test_gateway_alias_pagination():
    import httpx

    from sca.gateway.litellm import LiteLLMGateway

    pages = []

    def handle(request):
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(
            200,
            json={"keys": [{"key_alias": "task-one" if page == 1 else "other"}], "total_pages": 2},
        )

    gateway = LiteLLMGateway("http://gateway", "test", sandbox_url="http://sandbox")
    await gateway._http.aclose()
    gateway._http = httpx.AsyncClient(
        base_url="http://gateway", transport=httpx.MockTransport(handle)
    )
    try:
        assert await gateway.list_aliases("task-") == ["task-one"]
        assert pages == [1, 2]
    finally:
        await gateway.aclose()


async def test_maintenance_schedule_idempotent():
    from unittest.mock import AsyncMock

    from temporalio.client import ScheduleAlreadyRunningError, ScheduleOverlapPolicy

    from sca.config import Settings
    from sca.orchestration.worker import ensure_maintenance_schedule

    client = SimpleNamespace(create_schedule=AsyncMock())
    settings = Settings(maintenance_interval_s=123)
    await ensure_maintenance_schedule(client, settings)
    args = client.create_schedule.call_args.args
    assert args[0] == "sca-maintenance"
    assert args[1].policy.overlap == ScheduleOverlapPolicy.SKIP
    assert args[1].spec.intervals[0].every.total_seconds() == 123
    client.create_schedule.side_effect = ScheduleAlreadyRunningError()
    await ensure_maintenance_schedule(client, settings)
