import hmac
import json
import time
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, AliasPath, BaseModel, Field, TypeAdapter

from sca.billing.payments import TERMINATED_SUBSCRIPTIONS, StripeID, Subscription
from sca.tenancy.models import TenantStatus

SIGNATURE_TOLERANCE_S = 300
SUBSCRIPTION_EVENTS = frozenset({
    "customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted",
})
INVOICE_EVENTS = frozenset({"invoice.paid", "invoice.payment_failed"})
CHECKOUT_COMPLETED = "checkout.session.completed"
HANDLED_EVENTS = SUBSCRIPTION_EVENTS | INVOICE_EVENTS | {CHECKOUT_COMPLETED}
_stripe_reference = TypeAdapter(StripeID | None)


def verify_signature(
    payload: bytes, header: str, secret: str, *, now: float | None = None,
) -> bool:
    fields = [part.strip().partition("=") for part in header.split(",")]
    timestamps = [value for key, _, value in fields if key == "t"]
    if len(timestamps) != 1:
        return False
    timestamp = timestamps[0]
    if not timestamp.isascii() or not timestamp.isdecimal() or len(timestamp) > 12:
        return False
    if abs((time.time() if now is None else now) - int(timestamp)) > SIGNATURE_TOLERANCE_S:
        return False
    signed = timestamp.encode() + b"." + payload
    expected = hmac.digest(secret.encode(), signed, "sha256").hex().encode()
    return any(hmac.compare_digest(expected, value.encode()) for key, _, value in fields if key == "v1")


class _EventData(BaseModel):
    object: dict[str, Any]


class StripeEvent(BaseModel):
    id: StripeID
    type: str
    data: _EventData

    @classmethod
    def decode(cls, body: bytes) -> tuple["StripeEvent", dict[str, Any]]:
        payload = json.loads(body)
        return cls.model_validate(payload), payload

    @property
    def customer(self) -> str | None:
        return _stripe_reference.validate_python(self.data.object.get("customer"))

    def subscription_id(self) -> str | None:
        if self.type in SUBSCRIPTION_EVENTS:
            return _stripe_reference.validate_python(self.data.object.get("id"))
        if self.type == CHECKOUT_COMPLETED and self.data.object.get("mode") == "subscription":
            return _stripe_reference.validate_python(self.data.object.get("subscription"))
        return None


class Invoice(BaseModel):
    id: StripeID
    subscription_id: StripeID | None = Field(default=None, validation_alias=AliasChoices(
        AliasPath("parent", "subscription_details", "subscription"), "subscription",
    ))
    number: str | None = None
    amount_due: int
    amount_paid: int
    currency: str
    status: str | None = None
    attempt_count: int = Field(default=0, ge=0)
    hosted_invoice_url: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None

    @classmethod
    def from_event(cls, event: StripeEvent) -> "Invoice":
        invoice = cls.model_validate(event.data.object)
        invoice.status = "paid" if event.type == "invoice.paid" else (invoice.status or "open")
        return invoice


def follows_subscription(
    subscription_id: str | None, status: str | None, incoming: Subscription, plan: str | None,
) -> bool:
    if subscription_id == incoming.id:
        return status not in TERMINATED_SUBSCRIPTIONS or incoming.status in TERMINATED_SUBSCRIPTIONS
    return plan is not None and (
        subscription_id is None or (
            status in TERMINATED_SUBSCRIPTIONS and incoming.status not in TERMINATED_SUBSCRIPTIONS
        )
    )


def plan_from_subscription(status: str, mapped_plan: str | None) -> str | None:
    if status in {"active", "trialing"}:
        return mapped_plan or "free"
    return "free" if status == "canceled" else None


def invoice_suspension(
    invoice: Invoice, attempt_limit: int, subscription_status: str | None, overdue: bool,
) -> bool | None:
    if invoice.status == "paid":
        if subscription_status != "canceled" and not overdue:
            return False
    elif invoice.attempt_count >= attempt_limit:
        return True
    return None


def payment_suspension(
    tenant_status: TenantStatus, for_nonpayment: bool, suspend: bool,
) -> TenantStatus | None:
    if suspend and tenant_status == "active":
        return "suspended"
    if not suspend and tenant_status == "suspended" and for_nonpayment:
        return "active"
    return None
