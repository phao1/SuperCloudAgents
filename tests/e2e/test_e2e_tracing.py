"""One trace follows a task from the API request through the workflow into its activities."""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio.contrib.opentelemetry import TracingInterceptor

from .test_e2e_fake import RIGHT_FIX, block

pytestmark = pytest.mark.e2e

exporter = InMemorySpanExporter()


@pytest.fixture
def temporal_interceptors() -> list:
    # The global provider can be set once per process; this is the only test that sets it.
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return [TracingInterceptor()]


async def test_one_trace_spans_api_workflow_and_activities(stack, source_repo):
    task_id = await stack.submit(
        {
            "repo_url": str(source_repo),
            "instruction": f"Fix add.\n{block(RIGHT_FIX)}",
            "verify_command": "python -m pytest -q",
        }
    )
    task = await stack.wait_for(task_id, "AWAITING_REVIEW")
    trace_id = task["result"]["trace_id"]  # recorded at submit, for lookup in a trace viewer
    await stack.review(task_id, "approve")
    await stack.wait_for(task_id, "SUCCEEDED")

    spans = [
        s for s in exporter.get_finished_spans() if f"{s.context.trace_id:032x}" == trace_id
    ]
    names = {s.name for s in spans}
    print("\nspans in the task's trace:", sorted(names))
    assert any(n.startswith("POST /v1/tasks") for n in names)  # the API request
    assert "StartWorkflow:CodingTaskWorkflow" in names  # API -> Temporal
    assert "RunWorkflow:CodingTaskWorkflow" in names  # the workflow, in the worker
    for activity in ("prepare_workspace", "run_agent_turn", "run_verification", "publish"):
        assert f"RunActivity:{activity}" in names  # activities, in the worker
