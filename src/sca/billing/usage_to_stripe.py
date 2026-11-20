from datetime import date
from decimal import Decimal
from typing import Any

from sca.billing.payments import PaymentError, PaymentProvider
from sca.db.repo import Store


async def report_usage(
    store: Store, provider: PaymentProvider, month: date, markup: Decimal,
) -> dict[str, Any]:
    tenants = await store.prepare_billing_usage_reports(month, markup)
    sent, failures = 0, []
    for tenant_id in tenants:
        try:
            if await store.send_billing_usage_report(month, tenant_id, provider.create_usage_item):
                sent += 1
        except PaymentError:
            failures.append(tenant_id)
    return {"month": month.isoformat()[:7], "reported": sent, "failed_tenants": failures}
