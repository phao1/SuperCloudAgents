import csv
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from io import StringIO
from typing import Any, Literal

from sca.billing.metering import MICRO_USD

GroupBy = Literal["day", "model"]
USAGE_FIELDS = ("tasks", "turns", "tokens_in", "tokens_out", "sandbox_seconds", "cost_usd")


def _json_numbers(row: dict[str, Any]) -> dict[str, Any]:
    # JSON clients expect numbers; Decimal would otherwise serialize as a string.
    return {key: float(value) if isinstance(value, Decimal) else value for key, value in row.items()}


def aggregate(rows: list[dict[str, Any]], group_by: GroupBy) -> list[dict[str, Any]]:
    if group_by == "day":
        return [_json_numbers({key: value for key, value in row.items() if key != "by_model"})
                for row in rows]

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        for model, usage in row["by_model"].items():
            key = (row["tenant_id"], model)
            total = grouped.setdefault(key, dict.fromkeys(USAGE_FIELDS, 0))
            for metric in USAGE_FIELDS[:-1]:
                total[metric] += usage[metric]
            total["cost_usd"] += Decimal(usage["cost_micro_usd"]) / MICRO_USD
    return [_json_numbers({"tenant_id": tenant, "model": model, **total})
            for (tenant, model), total in sorted(grouped.items())]


def _spreadsheet_text(value: str) -> str:
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def billing_csv(
    tenant_id: str,
    plan: str,
    rows: list[dict[str, Any]],
    start: date,
    end: date,
    markup: Decimal,
    base_fee: Decimal,
) -> str:
    monthly: dict[date, dict[str, Any]] = defaultdict(lambda: dict.fromkeys(USAGE_FIELDS, 0))
    for row in rows:
        if start <= row["day"] <= end:
            bucket = monthly[row["day"].replace(day=1)]
            for metric in USAGE_FIELDS:
                bucket[metric] += row[metric]

    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["tenant_id", "month", "plan", *USAGE_FIELDS,
                     "markup", "base_fee_usd", "total_usd"])
    month, last_month = start.replace(day=1), end.replace(day=1)
    while month <= last_month:
        usage = monthly[month]
        total = (usage["cost_usd"] * markup + base_fee).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        writer.writerow([_spreadsheet_text(tenant_id), month.strftime("%Y-%m"),
                         _spreadsheet_text(plan), *(usage[key] for key in USAGE_FIELDS[:-1]),
                         format(usage["cost_usd"], "f"), format(markup, "f"),
                         format(base_fee, "f"), format(total, "f")])
        if month == last_month:
            break
        month = (month.replace(year=month.year + 1, month=1) if month.month == 12
                 else month.replace(month=month.month + 1))
    return stream.getvalue()
