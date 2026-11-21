from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response

from sca.api.authz import authorize
from sca.api.guest_history import history_payload
from sca.api.tenants import PlatformStore
from sca.billing.report import GroupBy, aggregate, billing_csv
from sca.config import get_settings
from sca.core.errors import InvalidTaskError
from sca.core.models import Principal

router = APIRouter(prefix="/v1")
UsageReader = Annotated[Principal, Depends(authorize("read"))]
HistoryReader = Annotated[Principal, Depends(authorize("read", guest=True))]


@dataclass(frozen=True, slots=True)
class UsageRange:
    start: date
    end: date


async def usage_range(
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> UsageRange:
    end = end or datetime.now(UTC).date()
    start = start or end.replace(day=1)
    if start > end:
        raise InvalidTaskError("用量起始日期必须早于或等于结束日期")
    return UsageRange(start, end)


Dates = Annotated[UsageRange, Depends(usage_range)]


@router.get("/usage")
async def tenant_usage(
    request: Request, user: HistoryReader, dates: Dates, group_by: GroupBy = "day",
) -> list[dict[str, Any]]:
    store = request.app.state.store.for_tenant(user.tenant_id)
    return history_payload(user, aggregate(await store.usage_rows(dates.start, dates.end), group_by))


@router.get("/admin/usage")
async def platform_usage(
    store: PlatformStore, dates: Dates, group_by: GroupBy = "day",
) -> list[dict[str, Any]]:
    return aggregate(await store.usage_rows(dates.start, dates.end), group_by)


@router.get("/usage/export.csv")
async def export_usage(request: Request, user: UsageReader, dates: Dates) -> Response:
    settings = get_settings()
    store = request.app.state.store.for_tenant(user.tenant_id)
    tenant = await store.get_tenant(user.tenant_id)
    plan = tenant["plan"]
    rows = await store.usage_rows(dates.start, dates.end)
    return Response(
        billing_csv(user.tenant_id, plan, rows, dates.start, dates.end, settings.billing_markup,
                    settings.billing_base_fees_usd.get(plan, 0)),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="usage.csv"', "Cache-Control": "no-store"},
    )
