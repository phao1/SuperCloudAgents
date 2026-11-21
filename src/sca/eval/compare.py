"""Paired comparisons with repeat-level spread and an exact sign test."""

from collections import defaultdict
from math import comb
from statistics import mean

from sca.eval.trajectory import overall_passed


def sign_test(wins: int, losses: int) -> float | None:
    n = wins + losses
    if n < 6:
        return None
    return min(1.0, 2 * sum(comb(n, k) for k in range(min(wins, losses) + 1)) / 2**n)


def summarize(rows: list[dict]) -> dict:
    repeats = defaultdict(list)
    for row in rows:
        if not row.get("infrastructure_error"):
            repeats[row["repeat"]].append({**row, "passed": overall_passed(row)})
    metrics = {"pass_rate": "passed", "acceptance_pass_rate": "acceptance_passed", "turns": "turns",
               "cost_usd": "cost_usd", "duration_s": "duration_s", "tokens": "tokens"}
    return {name: {"mean": mean(values), "min": min(values), "max": max(values)}
            for name, field in metrics.items()
            if (values := [mean(float(row.get(field) or 0) for row in group)
                           for group in repeats.values()])}


def compare(a: dict, b: dict) -> dict:
    if a["suite_hash"] != b["suite_hash"]:
        raise ValueError("cannot pair runs with different suite contents")
    cases_seen = {row["case_id"] for run in (a, b) for row in run["results"]}
    excluded = {(row["case_id"], row["repeat"]) for run in (a, b) for row in run["results"]
                if row.get("infrastructure_error")}
    grouped = []
    for run in (a, b):
        cases = defaultdict(list)
        for row in run["results"]:
            if (row["case_id"], row["repeat"]) not in excluded:
                cases[row["case_id"]].append(overall_passed(row))
        grouped.append(cases)
    table, wins, losses = [], 0, 0
    for case in sorted(cases_seen):
        rates = [mean(g[case]) if case in g else None for g in grouped]
        table.append({"case": case, "A": rates[0], "B": rates[1]})
        if None not in rates:
            wins += rates[1] > rates[0]
            losses += rates[1] < rates[0]
    p = sign_test(wins, losses)
    return {"A": summarize(a["results"]), "B": summarize(b["results"]), "cases": table,
            "infrastructure_errors": {side: sum(bool(row.get("infrastructure_error")) for row in run["results"])
                                      for side, run in (("A", a), ("B", b))},
            "B_wins": wins, "A_wins": losses, "p_value": p,
            "conclusion": "not enough data" if p is None else
            ("difference exceeds noise (p < 0.05)" if p < .05 else "difference may be noise")}


def markdown(report: dict) -> str:
    lines = ["| Metric (mean [min, max] over repeats) | A | B |", "|---|---|---|"]
    for metric in sorted(report["A"].keys() | report["B"].keys()):
        cells = []
        for side in ("A", "B"):
            value = report[side].get(metric)
            cells.append(f"{value['mean']:.4g} [{value['min']:.4g}, {value['max']:.4g}]" if value else "missing")
        lines.append(f"| {metric} | {' | '.join(cells)} |")
    lines += ["", "| Case | A pass rate | B pass rate |", "|---|---:|---:|"]
    for case in report["cases"]:
        lines.append(f"| {case['case']} | {case['A']} | {case['B']} |")
    errors = report.get("infrastructure_errors", {})
    if any(errors.values()):
        lines += ["", f"Infrastructure errors omitted from scoring (A: {errors['A']}, B: {errors['B']})."]
    lines += ["", report["conclusion"] + f"; paired sign test p={report['p_value']}."]
    return "\n".join(lines)
