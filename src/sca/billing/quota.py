from decimal import Decimal
from typing import Any

from sca.core.errors import ScaError


class PlanQuotaExceededError(ScaError):
    pass


def apply_cost_limits(
    spec: dict[str, Any],
    per_task: Decimal | None,
    monthly: Decimal | None,
    spent: Decimal,
) -> dict[str, Any]:
    requested = spec.get("max_cost_usd")
    limit = Decimal(str(requested)) if requested is not None else None
    if per_task is not None and limit is not None and limit > per_task:
        raise PlanQuotaExceededError(f"任务预算超过套餐上限 {per_task} 美元")
    remaining = None if monthly is None else monthly - spent
    if remaining is not None and remaining <= 0:
        raise PlanQuotaExceededError(f"租户本月已用完 {monthly} 美元额度")
    cap = min((value for value in (per_task, remaining, limit) if value is not None), default=None)
    if cap is None or cap == limit:
        return spec
    return {**spec, "max_cost_usd": float(cap)}
