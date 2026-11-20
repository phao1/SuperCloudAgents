from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sca.core.models import TERMINAL_STATES, Event

MICRO_USD = 1_000_000
COUNTERS = ("tasks", "turns", "tokens_in", "tokens_out", "sandbox_seconds", "cost_micro_usd")
METERED_EVENTS = (
    "agent.turn_started", "agent.turn_completed", "agent.usage", "model.key_issued",
    "sandbox.created", "sandbox.restored", "sandbox.released", "sandbox.destroyed",
    "sandbox.activity",
)
Scope = tuple[str, str]


def counters() -> dict[str, int]:
    return dict.fromkeys(COUNTERS, 0)


def nonnegative(value: Any) -> Decimal:
    try:
        value = Decimal(str(value))
    except InvalidOperation:
        return Decimal(0)
    return value if value.is_finite() and value >= 0 else Decimal(0)


def _scope(event: Event) -> Scope:
    # In-process children use the same task ID and tag their events to distinguish turn numbers.
    return (str(event.payload.get("subtask", "")), str(event.payload.get("best_of_candidate", "")))


def _model(event: Event) -> str | None:
    value = event.payload.get("model")
    return value if isinstance(value, str) and value else None


def completion_time(task: Mapping[str, Any], history: list[Event]) -> datetime:
    if task["finished_at"] is not None:
        return task["finished_at"]
    return next((event.created_at for event in history if event.type == "task.state_changed"
                 and event.payload.get("to") in TERMINAL_STATES), task["updated_at"])


def _sandbox_time(history: list[Event], finished_at: datetime) -> dict[Scope, Decimal]:
    opened: dict[Scope, datetime] = {}
    durations: dict[Scope, Decimal] = defaultdict(Decimal)

    def close(scope: Scope, when: datetime) -> None:
        if scope in opened:
            durations[scope] += nonnegative((when - opened.pop(scope)).total_seconds())

    for event in history:
        scope = _scope(event)
        if event.type in ("sandbox.created", "sandbox.restored"):
            close(scope, event.created_at)
            opened[scope] = event.created_at
        elif event.type == "sandbox.activity":
            if "sandbox_alive_s" in event.payload:
                durations[scope] += nonnegative(event.payload["sandbox_alive_s"])
                opened.pop(scope, None)
        elif event.type in ("sandbox.released", "sandbox.destroyed"):
            close(scope, event.created_at)
    for scope in list(opened):
        close(scope, finished_at)
    return durations


def task_usage(
    task: Mapping[str, Any], history: list[Event], finished_at: datetime, default_model: str,
) -> dict[str, Any]:
    default = task["spec"].get("model") or default_model
    turn_models = {
        (_scope(event), event.payload.get("turn")): model
        for event in history if event.type in ("model.key_issued", "agent.turn_started")
        if (model := _model(event)) is not None
    }
    scope_models: dict[Scope, set[str]] = defaultdict(set)
    for (scope, _), model in turn_models.items():
        scope_models[scope].add(model)
    starts = {(_scope(event), event.payload.get("turn"))
              for event in history if event.type == "agent.turn_started"}

    by_model: dict[str, dict[str, int]] = defaultdict(counters)
    for event in history:
        if event.type not in ("agent.turn_started", "agent.turn_completed", "agent.usage"):
            continue
        scope, turn = _scope(event), event.payload.get("turn")
        model = _model(event) or turn_models.get((scope, turn)) or default
        scope_models[scope].add(model)
        if event.type != "agent.usage":
            if event.type == "agent.turn_started" or (scope, turn) not in starts:
                by_model[model]["turns"] += 1
            continue
        for source, target in (
            ("input_tokens", "tokens_in"), ("output_tokens", "tokens_out"),
            ("cost_micro_usd", "cost_micro_usd"),
        ):
            by_model[model][target] += int(nonnegative(event.payload.get(source, 0)))

    for scope, seconds in _sandbox_time(history, finished_at).items():
        models = scope_models[scope]
        model = next(iter(models)) if len(models) == 1 else (default if not models else "unknown")
        by_model[model]["sandbox_seconds"] += round(seconds)

    if not by_model:
        by_model[default] = counters()
    # Older histories only store settled gateway spend on the parent task row. Never use
    # the backend's estimated "cost" as a charge or guess its split across multiple models.
    model = next(iter(by_model)) if len(by_model) == 1 else "unknown"
    for source, target in (
        ("input_tokens", "tokens_in"), ("output_tokens", "tokens_out"),
        ("cost_micro_usd", "cost_micro_usd"),
    ):
        total = int(nonnegative(task["usage"].get(source, 0)))
        accounted = sum(value[target] for value in by_model.values())
        if target == "cost_micro_usd" and accounted > total:
            for value in by_model.values():
                value[target] = 0
            accounted = 0
        if total > accounted:
            by_model[model][target] += total - accounted

    for value in by_model.values():
        value["tasks"] = 1
    return {
        **{key: sum(value[key] for value in by_model.values()) for key in COUNTERS},
        "tasks": 1,
        "by_model": dict(by_model),
    }


def daily_delta(
    row: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any],
) -> dict[str, Any]:
    def apply(total: Mapping[str, int], old: Mapping[str, int], new: Mapping[str, int]) -> dict:
        return {key: total.get(key, 0) - old.get(key, 0) + new.get(key, 0) for key in COUNTERS}

    total = apply({**row, "cost_micro_usd": int(row["cost_usd"] * MICRO_USD)}, before, after)
    before_models, after_models = before.get("by_model", {}), after["by_model"]
    by_model = dict(row["by_model"])
    for model in before_models.keys() | after_models.keys():
        value = apply(by_model.get(model, {}), before_models.get(model, {}),
                      after_models.get(model, {}))
        if any(value.values()):
            by_model[model] = value
        else:
            by_model.pop(model, None)
    cost = Decimal(total.pop("cost_micro_usd")) / MICRO_USD
    return {**total, "cost_usd": cost, "by_model": by_model}
