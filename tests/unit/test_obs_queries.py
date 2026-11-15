"""Keep deployed PromQL aligned with the exporters, without external services."""

import json
import re
from pathlib import Path

import pytest
import yaml
from prometheus_client import CollectorRegistry
from temporalio.runtime import PrometheusConfig

from sca import observability

ROOT = Path(__file__).resolve().parents[2]
# temporalio 1.36.1 SDK Core counter and label, confirmed in the installed bridge.
# worker.main uses PrometheusConfig defaults: temporal_ prefix, no _total suffix.
TEMPORAL_METRICS = {"temporal_activity_execution_failed": {"activity_type"}}
PROMETHEUS_METRICS = {"up": {"job", "instance"}}
FUNCTIONS = {"sum", "by", "rate", "increase", "clamp_min", "histogram_quantile", "absent"}
KEYWORDS = {"and", "or", "unless", "le", "state", "direction", "driver", "status", "activity_type"}


def queries() -> list[tuple[str, str]]:
    dashboard = json.loads((ROOT / "deploy/obs/grafana/dashboards/sca.json").read_text())
    result = [
        (f"{panel['title']}/{target['refId']}", target["expr"])
        for panel in dashboard["panels"]
        for target in panel["targets"]
    ]
    alerts = yaml.safe_load((ROOT / "deploy/obs/alerts.yml").read_text())
    result.extend(
        (rule["alert"], rule["expr"])
        for group in alerts["groups"]
        for rule in group["rules"]
    )
    return result


@pytest.fixture
def exported_metrics() -> dict[str, set[str]]:
    registry = CollectorRegistry()
    observability.Metrics(registry)
    observability.BusinessMetrics(registry)
    # Instantiate labelled series so collect() yields their actual exported sample names.
    for collector in list(registry._collector_to_names):
        labels = collector._labelnames
        if labels:
            collector.labels(*("example" for _ in labels))
    return {
        sample.name: set(sample.labels)
        for family in registry.collect()
        for sample in family.samples
    } | TEMPORAL_METRICS | PROMETHEUS_METRICS


def metric_names(expr: str) -> set[str]:
    # Remove strings and selectors first so label values cannot masquerade as metrics.
    clean = re.sub(r'"(?:\\.|[^"\\])*"', '""', expr)
    clean = re.sub(r"\{[^}]*\}", "", clean)
    names = set(re.findall(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(?=[\[)<>=!]|$)", clean))
    return names - FUNCTIONS - KEYWORDS


@pytest.mark.parametrize(("name", "expr"), queries(), ids=lambda value: value)
def test_query_metrics(name: str, expr: str, exported_metrics: dict[str, set[str]]) -> None:
    names = metric_names(expr)
    assert names, name
    assert names <= exported_metrics.keys(), (name, names - exported_metrics.keys())
    for metric, selector in re.findall(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\{([^}]*)\}", expr):
        labels = set(re.findall(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=~|!~|!=|=)", selector))
        assert labels <= exported_metrics[metric], (name, metric, labels)
    grouped_labels = {
        label.strip()
        for group in re.findall(r"\bby\s*\(([^)]*)\)", expr)
        for label in group.split(",")
    }
    assert grouped_labels <= set().union(*(exported_metrics[metric] for metric in names))


def test_metric_extraction() -> None:
    assert metric_names('sum(rate(unknown_metric{state="RUNNING"}[5m]))') == {"unknown_metric"}
    assert metric_names("unknown_gauge > 1") == {"unknown_gauge"}
    assert metric_names("sca_oldest_active_task_age_seconds") == {
        "sca_oldest_active_task_age_seconds"
    }


def test_temporal_export_defaults() -> None:
    config = PrometheusConfig(bind_address="127.0.0.1:9464")
    assert not config.counters_total_suffix
    assert not config.unit_suffix
