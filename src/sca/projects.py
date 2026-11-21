from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from sca.billing.metering import MICRO_USD, counters
from sca.core.models import TERMINAL_STATES, TaskState
from sca.core.projects import ProjectUsage, ThreadGroup, ThreadUsage, UsageCounters

WORKING_STATES = frozenset({
    TaskState.QUEUED, TaskState.PROVISIONING, TaskState.RUNNING, TaskState.VERIFYING,
})


def thread_group(row: Mapping[str, Any]) -> ThreadGroup:
    state = row["state"]
    if state in TERMINAL_STATES:
        return ThreadGroup.RESOLVED
    result, spec = row["result"], row["spec"]
    if state == TaskState.PAUSED or result.get("needs_input") or result.get("needs_approval"):
        return ThreadGroup.WAITING_ON_YOU
    if state == TaskState.AWAITING_REVIEW:
        human_selection = (spec.get("best_of") or {}).get("select") == "human"
        if row.get("plan_review") or (human_selection and result.get("best_of_chosen") is None):
            return ThreadGroup.WAITING_ON_YOU
        return ThreadGroup.READY_FOR_REVIEW
    if row.get("ci_pending") or (result.get("pr") or {}).get("state") in {"open", "merging"}:
        return ThreadGroup.LANDING
    if state in WORKING_STATES:
        return ThreadGroup.WORKING
    return ThreadGroup.IDLE


def prepare_thread_spec(
    kind: str, spec: dict, project: Mapping[str, Any],
    validate: Callable[[dict], dict] | None,
) -> dict:
    spec = dict(spec)
    for key, default in (("agent_backend", "default_backend"), ("model", "default_model")):
        if spec.get(key) is None and project[default] is not None:
            spec[key] = project[default]
    if kind == "coding" and project["repos"]:
        repos = project["repos"]
        spec.setdefault("repo_url", repos[0]["repo_url"])
        repo = next((repo for repo in repos if repo["repo_url"] == spec["repo_url"]), None)
        if repo is not None:
            spec.setdefault("base_ref", repo["base_ref"])
    if validate is not None:
        spec = validate(spec)
    instruction = "question" if kind == "analysis" else "instruction"
    if project["instructions"] and isinstance(spec.get(instruction), str):
        spec[instruction] = (
            f"## Project instructions\n\n{project['instructions']}\n\n"
            f"## Thread request\n\n{spec[instruction]}"
        )
    return spec


def _usage_view(values: Mapping[str, int]) -> UsageCounters:
    return UsageCounters(**values, cost_usd=values.get("cost_micro_usd", 0) / MICRO_USD)


def project_usage(
    rows: Iterable[tuple[Mapping[str, Any], dict[str, Any]]], default_model: str,
) -> ProjectUsage:
    totals = counters()
    by_model: dict[str, dict[str, int]] = defaultdict(counters)
    threads = []
    for row, usage in rows:
        models = usage["by_model"]
        threads.append(ThreadUsage(
            **_usage_view(usage).model_dump(), task_id=row["id"],
            model=row["spec"].get("model") or default_model,
            by_model={model: _usage_view(values) for model, values in models.items()},
        ))
        for key in totals:
            totals[key] += usage.get(key, 0)
        for model, values in models.items():
            for key in totals:
                by_model[model][key] += values.get(key, 0)
    return ProjectUsage(
        totals=_usage_view(totals), by_thread=threads,
        by_model={model: _usage_view(values) for model, values in by_model.items()},
    )
