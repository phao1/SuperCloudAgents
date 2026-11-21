from datetime import UTC, date, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from sca.api.payments import register_payments
from sca.billing.payments import (
    PaymentError,
    PaymentProvider,
    StripePaymentProvider,
    UsageInvoiceItem,
)
from sca.billing.stripe_events import Invoice, StripeEvent, verify_signature
from sca.billing.stripe_sim import DEFAULT_WEBHOOK_URL, StripeSimulator, sign_webhook
from sca.config import Settings

SIM_URL = "http://127.0.0.1:12111"
SCA_URL = "http://127.0.0.1:8000"


class WebhookReceiver:
    def __init__(self) -> None:
        self.events: list[StripeEvent] = []
        self.reject: str | None = None

    async def apply_stripe_event(
        self, event: StripeEvent, payload: dict, provider: PaymentProvider, attempt_limit: int,
    ) -> None:
        self.events.append(event)
        if event.type == self.reject:
            self.reject = None
            raise PaymentError()
        if subscription_id := event.subscription_id():
            subscription = await provider.get_subscription(subscription_id)
            assert subscription.customer == event.customer


@pytest.fixture
async def stripe():
    secret = uuid4().hex
    settings = Settings(_env_file=None, stripe_secret_key=uuid4().hex, stripe_webhook_secret=secret,
                        stripe_api_url=SIM_URL, api_url=SCA_URL)
    webhook = FastAPI()
    receiver = WebhookReceiver()
    webhook.state.settings = settings
    webhook.state.platform_store = receiver
    register_payments(webhook, settings, default_store=False)
    simulator = StripeSimulator(SIM_URL, DEFAULT_WEBHOOK_URL, secret,
                                webhook_transport=httpx.ASGITransport(app=webhook))
    transport = httpx.ASGITransport(app=simulator.app)
    provider = StripePaymentProvider(settings, transport)
    webhook.state.payment_provider = provider
    async with httpx.AsyncClient(transport=transport, base_url=SIM_URL) as client:
        yield client, provider, receiver


def test_simulator_signature_matches_the_webhook_verifier():
    payload, secret, timestamp = b'{"id": "evt_sim"}\n', uuid4().hex, 1_792_000_000
    signature = sign_webhook(payload, secret, timestamp=timestamp)
    assert signature.startswith(f"t={timestamp},v1=")
    assert verify_signature(payload, signature, secret, now=timestamp)
    assert not verify_signature(payload.rstrip(), signature, secret, now=timestamp)
    assert not verify_signature(payload, signature, secret, now=timestamp + 301)


async def test_checkout_expiry_browser_redirect_and_signed_subscription(stripe):
    client, provider, receiver = stripe
    customer = await provider.create_customer("acme")
    assert await provider.create_customer("acme") == customer
    expired = await provider.create_checkout(customer, "price_enterprise")
    await provider.expire_checkout(expired.id)
    await provider.expire_checkout(expired.id)
    assert (await provider.get_checkout(expired.id)).status == "expired"
    assert (await client.post(f"/_sim/checkout/{expired.id}/complete")).status_code == 409
    assert (await client.post("/_sim/checkout/cs_missing/complete")).status_code == 404
    assert receiver.events == []

    checkout = await provider.create_checkout(customer, "price_team")
    url = str(checkout.url)
    assert url == f"{SIM_URL}/_sim/checkout/{checkout.id}"
    page = await client.get(url)
    assert page.status_code == 200 and "<button>完成付款</button>" in page.text
    redirect = await client.post(url)
    assert redirect.status_code == 303
    assert redirect.headers["Location"] == f"{SCA_URL}/portal#/team"
    completed = await provider.get_checkout(checkout.id)
    assert completed.status == "complete" and completed.url is None and completed.subscription
    subscription = await provider.get_subscription(completed.subscription)
    assert (subscription.customer, subscription.status, subscription.price_id) == (customer, "active", "price_team")
    retry = await client.post(f"{url}/complete")
    assert retry.status_code == 200 and retry.json()["subscription"] == subscription.id
    assert [event.type for event in receiver.events] == ["checkout.session.completed", "customer.subscription.created"]


async def test_failed_webhook_delivery_retries_without_a_new_subscription_or_attempt(stripe):
    client, provider, receiver = stripe
    checkout = await provider.create_checkout(await provider.create_customer("acme"), "price_team")
    receiver.reject = "customer.subscription.created"
    assert (await client.post(str(checkout.url))).status_code == 502
    completed = await provider.get_checkout(checkout.id)
    assert completed.status == "complete" and completed.subscription
    retried = await client.post(f"{checkout.url}/complete")
    assert retried.status_code == 200 and retried.json()["subscription"] == completed.subscription
    checkout_event, creation, creation_retry = receiver.events
    assert checkout_event.type == "checkout.session.completed" and creation.id == creation_retry.id

    path = f"/_sim/subscriptions/{completed.subscription}"
    receiver.reject = "invoice.payment_failed"
    assert (await client.post(f"{path}/payment_failed")).status_code == 502
    failure = receiver.events[-1]
    assert (await client.post(f"{path}/payment_succeeded")).status_code == 409
    retry = await client.post(f"{path}/payment_failed")
    assert retry.status_code == 200 and retry.json()["attempt_count"] == 1
    assert receiver.events[-1].id == failure.id


async def test_invoice_payments_usage_overages_and_subscription_lifecycle(stripe):
    client, provider, receiver = stripe
    customer = await provider.create_customer("acme")
    checkout = await provider.create_checkout(customer, "price_team")
    completed = await client.post(f"{checkout.url}/complete")
    assert completed.status_code == 200
    sub_id = completed.json()["subscription"]
    path = f"/_sim/subscriptions/{sub_id}"
    item = UsageInvoiceItem(tenant_id="acme", month=date(2025, 10, 1), stripe_customer_id=customer,
                            subscription_id=sub_id, amount_cents=152, created_at=datetime.now(UTC))
    first = await provider.create_usage_item(item)
    second = await provider.create_usage_item(item.model_copy(update={"month": date(2025, 11, 1)}))
    assert await provider.create_usage_item(item) == first
    params = {"customer": customer, "limit": 1}
    page = (await client.get("/v1/invoiceitems", params=params)).json()
    assert [row["id"] for row in page["data"]] == [second] and page["has_more"]
    rest = (await client.get("/v1/invoiceitems", params={**params, "starting_after": second})).json()
    assert [row["id"] for row in rest["data"]] == [first] and not rest["has_more"]

    invoices = []
    for attempt in (1, 2, 3):
        failed = await client.post(f"{path}/payment_failed")
        assert failed.status_code == 200
        invoice = Invoice.from_event(receiver.events[-1])
        assert invoice.attempt_count == attempt and invoice.status == "open" and not invoice.amount_paid
        assert invoice.subscription_id == sub_id and invoice.id == failed.json()["id"]
        invoices.append(invoice.id)
    assert len(set(invoices)) == 1 and (await provider.get_subscription(sub_id)).status == "past_due"
    paid = await client.post(f"{path}/payment_succeeded")
    assert paid.status_code == 200
    paid_invoice = Invoice.from_event(receiver.events[-1])
    assert paid_invoice.id == invoices[0] and paid_invoice.status == "paid"
    assert paid_invoice.amount_paid == paid_invoice.amount_due == 2900 + 2 * 152
    assert (await provider.get_subscription(sub_id)).status == "active"
    retry_paid = await client.post(f"{path}/payment_succeeded")
    assert retry_paid.json() == paid.json()

    portal = await client.get(await provider.create_portal(customer))
    assert portal.status_code == 200 and paid_invoice.hosted_invoice_url in portal.text
    hosted = await client.get(paid_invoice.hosted_invoice_url)
    assert hosted.status_code == 200 and "已付款" in hosted.text
    changed = await client.post(f"{path}/update", json={"price_id": "price_enterprise"})
    assert changed.status_code == 200 and (await provider.get_subscription(sub_id)).price_id == "price_enterprise"
    canceled = await client.post(f"{path}/cancel")
    assert canceled.status_code == 200 and (await provider.get_subscription(sub_id)).status == "canceled"
    assert (await client.post(f"{path}/payment_failed")).status_code == 409
    assert [event.type for event in receiver.events] == [
        "checkout.session.completed", "customer.subscription.created", "customer.subscription.updated",
        "invoice.payment_failed", "invoice.payment_failed", "invoice.payment_failed",
        "customer.subscription.updated", "invoice.paid", "customer.subscription.updated",
        "customer.subscription.deleted",
    ]
