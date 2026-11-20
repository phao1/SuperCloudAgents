import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4

import httpx
from pydantic import AnyHttpUrl, BaseModel, BeforeValidator, StringConstraints

from sca.config import Settings

STRIPE_VERSION = "2025-03-31.basil"
TERMINATED_SUBSCRIPTIONS = frozenset({"canceled", "incomplete_expired"})
BILLABLE_SUBSCRIPTIONS = frozenset({"active", "trialing", "past_due", "unpaid"})


class PaymentError(Exception):
    def __init__(self) -> None:
        super().__init__("无法完成 Stripe 请求，请稍后重试")


def _reference_id(value: Any) -> Any:
    return value.get("id") if isinstance(value, dict) else value


StripeID = Annotated[
    str, StringConstraints(pattern=r"^[a-zA-Z0-9_-]{1,255}$"), BeforeValidator(_reference_id),
]


class _StripeResource(BaseModel):
    id: StripeID


class _StripeLink(BaseModel):
    url: AnyHttpUrl


class CheckoutSession(_StripeResource):
    status: Literal["open", "complete", "expired"]
    url: AnyHttpUrl | None = None
    subscription: StripeID | None = None

    def checkout_url(self) -> str:
        if self.url is None or self.status != "open":
            raise PaymentError()
        return str(self.url)


class _SubscriptionItem(BaseModel):
    price: StripeID
    current_period_end: datetime | None = None


class _SubscriptionItems(BaseModel):
    data: list[_SubscriptionItem]
    has_more: bool = False


class Subscription(_StripeResource):
    customer: StripeID
    status: str
    items: _SubscriptionItems
    current_period_end: datetime | None = None

    @property
    def price_id(self) -> str | None:
        # Checkout and the billing portal sell single-price subscriptions.
        if len(self.items.data) == 1 and not self.items.has_more:
            return self.items.data[0].price
        return None

    @property
    def renewal(self) -> datetime | None:
        if self.status in TERMINATED_SUBSCRIPTIONS or self.price_id is None:
            return None
        return self.items.data[0].current_period_end or self.current_period_end


class UsageInvoiceItem(BaseModel):
    tenant_id: str
    month: date
    stripe_customer_id: StripeID
    subscription_id: StripeID
    amount_cents: int
    created_at: datetime

    @property
    def key(self) -> str:
        return _idempotency_key("usage", f"{self.stripe_customer_id}:{self.tenant_id}:{self.month}")


class PaymentProvider(Protocol):
    async def create_customer(self, tenant_id: str) -> str: ...
    async def create_checkout(self, customer_id: str, price_id: str) -> CheckoutSession: ...
    async def get_checkout(self, session_id: str) -> CheckoutSession: ...
    async def expire_checkout(self, session_id: str) -> None: ...
    async def create_portal(self, customer_id: str) -> str: ...
    async def get_subscription(self, subscription_id: str) -> Subscription: ...
    async def create_usage_item(self, item: UsageInvoiceItem) -> str: ...


def _idempotency_key(operation: str, identity: str) -> str:
    return f"sca-{operation}-{hashlib.sha256(identity.encode()).hexdigest()}"


class StripePaymentProvider:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        if not settings.stripe_secret_key:
            raise ValueError("SCA_STRIPE_SECRET_KEY is required")
        self._settings = settings
        self._key = settings.stripe_secret_key
        self._transport = transport

    async def _request[T: BaseModel](
        self, path: str, model: type[T], *, form: dict[str, str] | None = None,
        key: str | None = None, params: dict[str, str] | None = None,
    ) -> T:
        headers = {
            "Authorization": f"Bearer {self._key.get_secret_value()}",
            "Stripe-Version": STRIPE_VERSION,
        }
        if key is not None:
            headers["Idempotency-Key"] = key
        try:
            async with httpx.AsyncClient(
                base_url=str(self._settings.stripe_api_url), headers=headers, timeout=8,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    "GET" if form is None else "POST", path, data=form, params=params,
                )
                response.raise_for_status()
                return model.model_validate(response.json())
        except (httpx.HTTPError, ValueError):
            # Stripe errors and response bodies can include payment details.
            raise PaymentError() from None

    async def create_customer(self, tenant_id: str) -> str:
        customer = await self._request(
            "/v1/customers", _StripeResource, form={"metadata[sca_tenant_id]": tenant_id},
            key=_idempotency_key("customer", f"{self._settings.api_url.rstrip('/')}:{tenant_id}"),
        )
        return customer.id

    async def create_checkout(self, customer_id: str, price_id: str) -> CheckoutSession:
        return await self._request(
            "/v1/checkout/sessions", CheckoutSession, key=f"sca-checkout-{uuid4()}", form={
                "customer": customer_id,
                "mode": "subscription",
                "line_items[0][price]": price_id,
                "line_items[0][quantity]": "1",
                "adaptive_pricing[enabled]": "false",
                "success_url": str(self._settings.billing_success_url),
                "cancel_url": str(self._settings.billing_cancel_url),
            },
        )

    async def get_checkout(self, session_id: str) -> CheckoutSession:
        return await self._request(f"/v1/checkout/sessions/{session_id}", CheckoutSession)

    async def expire_checkout(self, session_id: str) -> None:
        await self._request(
            f"/v1/checkout/sessions/{session_id}/expire", CheckoutSession,
            key=f"sca-expire-checkout-{session_id}", form={},
        )

    async def create_portal(self, customer_id: str) -> str:
        portal = await self._request(
            "/v1/billing_portal/sessions", _StripeLink, key=f"sca-portal-{uuid4()}",
            form={"customer": customer_id, "return_url": str(self._settings.billing_success_url)},
        )
        return str(portal.url)

    async def get_subscription(self, subscription_id: str) -> Subscription:
        return await self._request(f"/v1/subscriptions/{subscription_id}", Subscription)

    async def create_usage_item(self, item: UsageInvoiceItem) -> str:
        existing = await self._find_usage_item(item)
        if existing is not None:
            return existing

        from sca.billing.report import month_end

        start = datetime.combine(item.month, datetime.min.time(), UTC)
        end = datetime.combine(month_end(item.month), datetime.max.time(), UTC)
        created = await self._request("/v1/invoiceitems", _StripeResource, key=item.key, form={
            "customer": item.stripe_customer_id,
            "subscription": item.subscription_id,
            "currency": "usd",
            "amount": str(item.amount_cents),
            "description": f"SCA usage overage {item.month:%Y-%m}",
            "period[start]": str(int(start.timestamp())),
            "period[end]": str(int(end.timestamp())),
            "metadata[sca_usage_key]": item.key,
        })
        return created.id

    async def _find_usage_item(self, item: UsageInvoiceItem) -> str | None:
        # Stripe can expire POST idempotency keys after 24 hours. Recover an item if
        # a previous run reached Stripe but did not save its ID locally.
        after = item.created_at - timedelta(minutes=5)
        params = {"customer": item.stripe_customer_id, "created[gte]": str(int(after.timestamp())),
                  "limit": "100"}
        while True:
            page = await self._request("/v1/invoiceitems", _InvoiceItemPage, params=params)
            for existing in page.data:
                if existing.metadata.get("sca_usage_key") == item.key:
                    return existing.id
            if not page.has_more:
                return None
            if not page.data or params.get("starting_after") == page.data[-1].id:
                raise PaymentError()
            params["starting_after"] = page.data[-1].id


class _StripeInvoiceItem(_StripeResource):
    metadata: dict[str, str]


class _InvoiceItemPage(BaseModel):
    data: list[_StripeInvoiceItem]
    has_more: bool
