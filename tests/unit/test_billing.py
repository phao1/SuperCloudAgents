import csv
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO
from uuid import uuid4

import pytest

from sca.admission.order import tenant_slot_limit
from sca.billing.metering import daily_delta, task_usage
from sca.billing.quota import PlanQuotaExceededError, apply_cost_limits
from sca.billing.report import aggregate, billing_csv
from sca.core.models import Event


def test_export_groups_months_and_charges_base_fee_once():
    def row(day: date, cost: str) -> dict:
        return {"day": day, "cost_usd": Decimal(cost), "tasks": 1, "turns": 2,
                "tokens_in": 10, "tokens_out": 5, "sandbox_seconds": 30}

    rows = [row(date(2026, 10, 1), "25"), row(date(2026, 10, 30), "0.01"),
            row(date(2026, 10, 31), "1.20"), row(date(2026, 12, 1), "0.333333")]
    content = billing_csv("a", "team", rows, date(2026, 10, 30), date(2026, 12, 1),
                          Decimal("1.5"), Decimal(4))
    oct_, nov, dec = csv.DictReader(StringIO(content))
    assert oct_ == {
        "tenant_id": "a", "month": "2026-10", "plan": "team", "tasks": "2", "turns": "4",
        "tokens_in": "20", "tokens_out": "10", "sandbox_seconds": "60", "cost_usd": "1.21",
        "markup": "1.5", "base_fee_usd": "4", "total_usd": "5.82",
    }
    assert (nov["tasks"], nov["total_usd"], dec["total_usd"]) == ("0", "4.00", "4.50")


def test_export_quotes_legacy_identifiers_safely():
    day = date(2026, 1, 1)
    content = billing_csv('=test,"name"', "enterprise", [], day, day, Decimal(1), Decimal(0))
    [row] = csv.DictReader(StringIO(content))
    assert row["tenant_id"] == "'=test,\"name\""
    assert row["total_usd"] == "0.00"


def test_plan_limits_apply_to_defaults_and_request_budgets():
    monthly, per_task = Decimal(10), Decimal(1)
    original = {"instruction": "fix"}
    assert apply_cost_limits(original, per_task, monthly, Decimal(0))["max_cost_usd"] == 1
    assert apply_cost_limits(original, per_task, monthly, Decimal("9.75"))["max_cost_usd"] == 0.25
    assert "max_cost_usd" not in original
    explicit = {"max_cost_usd": 0.1}
    assert apply_cost_limits(explicit, per_task, monthly, Decimal("9.75")) == explicit
    assert apply_cost_limits(original, None, None, Decimal(10)) == original
    with pytest.raises(PlanQuotaExceededError, match="任务预算"):
        apply_cost_limits({"max_cost_usd": 2}, per_task, monthly, Decimal(0))
    with pytest.raises(PlanQuotaExceededError, match="本月"):
        apply_cost_limits(original, per_task, monthly, monthly)
    assert tenant_slot_limit(20, 4) == 4
    assert tenant_slot_limit(2, 4) == 2
    assert tenant_slot_limit(20, None) == 20


def test_metering_combines_children_and_applies_late_updates_once():
    task_id = uuid4()
    now = datetime(2026, 11, 20, tzinfo=UTC)
    history: list[Event] = []

    def add(type_: str, second: int, **payload: object) -> None:
        history.append(Event(task_id=task_id, seq=len(history) + 1, type=type_,
                             created_at=now + timedelta(seconds=second), payload=payload))

    add("sandbox.created", 0)
    add("sandbox.created", 1, subtask="a")
    add("sandbox.created", 2, best_of_candidate="runner-up")
    add("agent.turn_started", 3, turn=1)
    add("agent.turn_started", 3, turn=1, subtask="a")
    add("agent.turn_started", 3, turn=1, best_of_candidate="runner-up")
    add("model.key_issued", 3, turn=1, model="m2", best_of_candidate="runner-up")
    add("agent.usage", 4, turn=1, input_tokens=2, output_tokens=1, cost=200)
    add("agent.usage", 4, turn=1, subtask="a", input_tokens=5, output_tokens=2)
    add("agent.usage", 5, turn=1, best_of_candidate="runner-up",
        input_tokens=8, output_tokens=4, cost_micro_usd=100_000)
    add("agent.turn_completed", 6, turn=1, subtask="a")
    add("sandbox.destroyed", 7, subtask="a")
    add("sandbox.destroyed", 8, best_of_candidate="runner-up")

    task = {"spec": {"model": "m1"}, "usage": {"cost_micro_usd": 300_000}}
    finished = now + timedelta(seconds=10)
    initial = task_usage(task, history, finished, "default")
    assert {key: initial[key] for key in ("tasks", "turns", "tokens_in", "tokens_out",
                                         "cost_micro_usd", "sandbox_seconds")} == {
        "tasks": 1, "turns": 3, "tokens_in": 15, "tokens_out": 7, "cost_micro_usd": 300_000,
        "sandbox_seconds": 22,
    }
    assert initial["by_model"]["m2"]["cost_micro_usd"] == 100_000
    assert initial["by_model"]["unknown"]["cost_micro_usd"] == 200_000
    assert initial["by_model"]["m1"]["tokens_in"] == 7
    day = daily_delta({"cost_usd": Decimal(0), "by_model": {}}, {}, initial)
    add("sandbox.activity", 11, sandbox_alive_s=12)
    add("sandbox.destroyed", 12)
    final = task_usage(task, history, finished, "default")
    day = daily_delta(day, initial, final)
    assert day == daily_delta(day, final, final)
    assert day["tasks"] == 1
    assert day["sandbox_seconds"] == 24
    assert day["cost_usd"] == Decimal("0.3")
    models = aggregate([{"tenant_id": "a", "day": now.date(), **day}], "model")
    assert {row["model"]: row["cost_usd"] for row in models} == {
        "m1": 0.0, "m2": 0.1, "unknown": 0.2,
    }
