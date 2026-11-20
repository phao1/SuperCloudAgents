import hmac
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from sca.api.app import create_app
from sca.billing.payments import PaymentError, StripePaymentProvider, Subscription, UsageInvoiceItem
from sca.billing.report import USAGE_FIELDS, overage_cents
from sca.billing.stripe_events import (
    Invoice,
    StripeEvent,
    follows_subscription,
    invoice_suspension,
    payment_suspension,
    verify_signature,
)
from sca.cli import app as cli
from sca.config import Settings
from sca.db.repo import Store


def signature(timestamp: int, body: bytes, secret: str) -> str:
    digest = hmac.digest(secret.encode(), f"{timestamp}.".encode() + body, "sha256").hex()
    return f"t={timestamp},v1={digest}"


def test_stripe_webhook_signature_uses_raw_bytes_and_fresh_timestamp():
    secret, timestamp = uuid4().hex, 1_792_000_000
    body = b'{"id": "evt_test"}\n'
    header = signature(timestamp, body, secret)
    assert verify_signature(body, header + ",v1=bad", secret, now=timestamp + 300)
    assert verify_signature(body, header, secret, now=timestamp - 300)
    assert not verify_signature(body, header, uuid4().hex, now=timestamp)
    assert not verify_signature(body.strip(), header, secret, now=timestamp)
    assert not verify_signature(body, header, secret, now=timestamp + 300.1)
    assert not verify_signature(body, header, secret, now=timestamp - 301)
    assert not verify_signature(body, header.replace("v1=", "v0="), secret, now=timestamp)
    assert not verify_signature(body, header + f",t={timestamp}", secret, now=timestamp)
    assert not verify_signature(body, "t=garbled,v1=bad", secret, now=timestamp)


def event(type_: str, **obj) -> StripeEvent:
    return StripeEvent.model_validate({"id": "evt_test", "type": type_, "data": {"object": obj}})


def subscription(status="active", price="price_team", id_="sub_test") -> dict:
    return {"id": id_, "customer": "cus_acme", "status": status,
            "items": {"data": [{"price": {"id": price}, "current_period_end": 1_792_010_000}]}}


def test_subscription_and_invoice_compatibility_and_suspension():
    current = Subscription.model_validate(subscription())
    assert current.price_id == "price_team" and current.renewal == datetime.fromtimestamp(1_792_010_000, UTC)
    legacy = Subscription.model_validate({**subscription(), "items": {"data": [{"price": "price_team"}]},
                                          "current_period_end": 1_792_020_000})
    assert legacy.renewal == datetime.fromtimestamp(1_792_020_000, UTC)
    assert not follows_subscription("sub_old", "active", current, "team")
    assert not follows_subscription("sub_test", "canceled", current, "team")
    assert not follows_subscription(None, None, current, None)
    assert follows_subscription("sub_old", "canceled", current, "team")
    assert follows_subscription("sub_test", "past_due", current, "team")

    fields = {"id": "in_1", "customer": "cus_acme", "amount_due": 100, "amount_paid": 0, "currency": "usd"}
    failed = Invoice.from_event(event("invoice.payment_failed", **fields, subscription="sub_test", attempt_count=3))
    paid = Invoice.from_event(event("invoice.paid", **fields, parent={"subscription_details": {
        "subscription": {"id": "sub_test"},
    }}))
    assert failed.subscription_id == paid.subscription_id == "sub_test"
    assert invoice_suspension(failed, 3, "past_due", False) is True
    assert invoice_suspension(failed, 4, "past_due", False) is None
    assert invoice_suspension(paid, 3, "past_due", True) is None
    assert invoice_suspension(paid, 3, "canceled", False) is None
    assert invoice_suspension(paid, 3, "past_due", False) is False
    assert payment_suspension("suspended", False, suspend=False) is None
    assert payment_suspension("suspended", False, suspend=True) is None


async def test_webhook_replay_is_acknowledged_without_reapplying_state(monkeypatch, tmp_path):
    secret = uuid4().hex
    settings = Settings(_env_file=None, data_dir=tmp_path, stripe_secret_key=uuid4().hex,
                        stripe_webhook_secret=secret, stripe_api_url="https://stripe.example")
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    session = AsyncMock()
    session.scalar.side_effect = ["acme", "evt_test", "acme", None]

    @asynccontextmanager
    async def transaction():
        yield session

    bind = SimpleNamespace(url=make_url("postgresql+asyncpg://localhost/sca"))
    store = Store(SimpleNamespace(kw={"bind": bind}))
    monkeypatch.setattr(store, "_transaction", transaction)
    monkeypatch.setattr(store, "_find_tenant", AsyncMock(return_value={"deleted_at": None}))
    monkeypatch.setattr(store, "_billing_customer", AsyncMock(return_value={"tenant_id": "acme"}))
    project = AsyncMock()
    monkeypatch.setattr(store, "_project_stripe_event", project)
    app = create_app(store=store)
    body = json.dumps(event("customer.subscription.updated", **subscription()).model_dump()).encode()
    now = int(time.time())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = "/v1/billing/stripe/webhook"
        assert (await client.post(path, content=body)).status_code == 400
        assert (await client.post(path, content=body, headers={"Stripe-Signature": signature(now - 301, body, secret)})).status_code == 400
        assert session.scalar.await_count == 0
        for timestamp in (now, now + 1):
            replay = await client.post(path, content=body, headers={"Stripe-Signature": signature(timestamp, body, secret)})
            assert replay.status_code == 200 and replay.json() == {"received": True}
    assert project.await_count == 1
    inserts = [session.scalar.await_args_list[i].args[0] for i in (1, 3)]
    assert all("ON CONFLICT DO NOTHING" in str(stmt) for stmt in inserts)
    assert [stmt.compile().params["id"] for stmt in inserts] == ["evt_test", "evt_test"]


async def test_billing_webhook_state_machine_with_fake_stripe(monkeypatch):
    tenant = {"id": "acme", "plan": "free", "status": "active"}
    customer = {"tenant_id": "acme", "stripe_customer_id": "cus_acme", "subscription_id": None,
                "subscription_status": None, "price_id": None, "current_period_end": None,
                "suspended_for_nonpayment": False}
    live = subscription()

    def stripe(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/v1/subscriptions/sub_test"
        return httpx.Response(200, json=live)

    settings = Settings(_env_file=None, stripe_secret_key=uuid4().hex, stripe_api_url="https://stripe.example")
    provider = StripePaymentProvider(settings, httpx.MockTransport(stripe))
    store = Store(None)
    session = AsyncMock()

    async def resolve_plan(query):
        [price] = query.compile().params.values()
        return {"price_team": "team", "price_enterprise": "enterprise"}.get(price)

    async def update_billing(statement):
        customer.update({k: v for k, v in statement.compile().params.items() if k in customer})

    async def update_tenant(session, tenant_id, **changes):
        assert tenant_id == tenant["id"]
        tenant.update(changes)

    session.scalar.side_effect = resolve_plan
    session.execute.side_effect = update_billing
    monkeypatch.setattr(store, "_update_tenant_row", update_tenant)
    monkeypatch.setattr(store, "_record_billing_invoice", AsyncMock(return_value=True))
    monkeypatch.setattr(store, "_billing_invoice_overdue", AsyncMock(return_value=False))

    async def apply(type_: str, **obj):
        await store._project_stripe_event(session, tenant, customer, event(type_, **obj), provider, 3)

    await apply("checkout.session.completed", mode="subscription", subscription="sub_test")
    assert (tenant["plan"], customer["subscription_status"]) == ("team", "active")
    live = subscription(price="price_enterprise")
    await apply("customer.subscription.updated", **subscription())
    assert tenant["plan"] == "enterprise"
    fields = {"id": "in_1", "subscription": "sub_test", "amount_due": 100, "amount_paid": 0, "currency": "usd"}
    for attempt in (1, 2, 3):
        await apply("invoice.payment_failed", **fields, attempt_count=attempt)
        assert customer["suspended_for_nonpayment"] == (attempt == 3)
    assert tenant["status"] == "suspended"
    await apply("invoice.paid", **fields)
    assert tenant["status"] == "active" and not customer["suspended_for_nonpayment"]

    standalone = {**fields, "id": "in_usage", "subscription": None}
    await apply("invoice.payment_failed", **standalone, attempt_count=3)
    assert tenant["status"] == "suspended" and customer["suspended_for_nonpayment"]
    await apply("invoice.paid", **standalone)
    assert tenant["status"] == "active"

    live = subscription("unpaid", price="price_enterprise")
    await apply("customer.subscription.updated", **live)
    assert tenant["status"] == "suspended" and customer["suspended_for_nonpayment"]
    await apply("customer.subscription.deleted", **live)
    assert (tenant["plan"], customer["subscription_status"], customer["current_period_end"]) == (
        "free", "canceled", None,
    )
    await apply("invoice.paid", **fields)
    assert tenant["status"] == "suspended"


def test_monthly_overage_excludes_other_months_and_rounds_to_cents():
    def usage(day: date, cost: str) -> dict:
        return {**dict.fromkeys(USAGE_FIELDS, 0), "day": day, "cost_usd": Decimal(cost)}

    month = date(2024, 2, 1)
    rows = [usage(date(2024, 1, 31), "100"), usage(month, "4"), usage(date(2024, 2, 29), "1.01"),
            usage(date(2024, 3, 1), "100")]
    assert overage_cents(month, rows, Decimal(4), Decimal("1.5")) == 152
    assert overage_cents(month, rows, Decimal("5.01"), Decimal(2)) == 0
    assert overage_cents(month, rows, Decimal(6), Decimal(2)) == 0
    assert overage_cents(month, [], Decimal(0), Decimal(2)) == 0


async def test_checkout_and_portal_use_form_encoded_stripe_requests():
    seen = []

    def stripe(request):
        assert request.method == "POST" and request.url.host == "stripe.example"
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        seen.append((request.url.path, request.headers["Idempotency-Key"], form))
        return httpx.Response(200, json={
            "/v1/customers": {"id": "cus_test"},
            "/v1/checkout/sessions": {"id": "cs_test", "status": "open", "url": "https://checkout.example/test"},
            "/v1/billing_portal/sessions": {"url": "https://billing.example/test"},
        }[request.url.path])

    settings = Settings(_env_file=None, stripe_secret_key=uuid4().hex, stripe_api_url="https://stripe.example",
                        api_url="https://sca.example/", billing_cancel_url="https://sca.example/cancelled")
    provider = StripePaymentProvider(settings, httpx.MockTransport(stripe))
    assert await provider.create_customer("acme") == await provider.create_customer("acme") == "cus_test"
    assert seen[0] == seen[1]
    checkout = await provider.create_checkout("cus_test", "price_team")
    assert checkout.checkout_url() == "https://checkout.example/test"
    assert seen[-1][2] == {
        "customer": "cus_test", "mode": "subscription", "line_items[0][price]": "price_team",
        "line_items[0][quantity]": "1", "adaptive_pricing[enabled]": "false",
        "success_url": "https://sca.example/portal#/team", "cancel_url": "https://sca.example/cancelled",
    }
    assert await provider.create_portal("cus_test") == "https://billing.example/test"
    assert seen[-1][2] == {"customer": "cus_test", "return_url": "https://sca.example/portal#/team"}


async def test_usage_report_reserves_amount_and_skips_a_sent_row(monkeypatch):
    store = Store(None)
    session = AsyncMock()
    month = date(2025, 2, 1)
    account = {"tenant_id": "acme", "stripe_customer_id": "cus_acme", "subscription_id": "sub_acme"}
    report = {**account, "month": month, "amount_cents": 152, "created_at": datetime(2025, 3, 1, tzinfo=UTC)}
    candidates, pending, sent = Mock(), Mock(), Mock()
    candidates.mappings.return_value.all.return_value = [{**account, "included": Decimal(4)}]
    pending.mappings.return_value.one_or_none.return_value = report
    sent.mappings.return_value.one_or_none.return_value = None
    session.execute.side_effect = [candidates, None, pending, None, sent]
    session.scalars.return_value = ["acme"]

    @asynccontextmanager
    async def transaction():
        yield session

    usage = AsyncMock(return_value=[{
        **dict.fromkeys(USAGE_FIELDS, 0), "tenant_id": "acme", "day": month, "cost_usd": Decimal("5.01"),
    }])
    monkeypatch.setattr(store, "_transaction", transaction)
    monkeypatch.setattr(store, "usage_rows", usage)
    assert await store.prepare_billing_usage_reports(month, Decimal("1.5")) == ["acme"]
    usage.assert_awaited_once_with(month, date(2025, 2, 28))
    query, insert = [call.args[0].compile(dialect=postgresql.dialect()) for call in session.execute.await_args_list]
    assert "JOIN plans ON plans.stripe_price_id = billing_customers.price_id" in str(query)
    assert "ON CONFLICT DO NOTHING" in str(insert) and insert.params["amount_cents"] == 152
    create_item = AsyncMock(return_value="ii_test")
    assert await store.send_billing_usage_report(month, "acme", create_item)
    assert not await store.send_billing_usage_report(month, "acme", create_item)
    create_item.assert_awaited_once_with(UsageInvoiceItem.model_validate(report))
    recorded = session.execute.await_args_list[3].args[0].compile(dialect=postgresql.dialect())
    assert recorded.params["stripe_invoice_item_id"] == "ii_test"


async def test_usage_invoice_item_recovers_after_stripe_accepts_post():
    item = UsageInvoiceItem(
        tenant_id="acme", month=date(2025, 2, 1), amount_cents=152, stripe_customer_id="cus_acme",
        subscription_id="sub_test", created_at=datetime(2025, 3, 1, tzinfo=UTC),
    )
    stored, posts = [], []

    def stripe(request):
        assert request.url.path == "/v1/invoiceitems" and request.url.host == "stripe.example"
        if request.method == "GET":
            assert request.url.params["customer"] == "cus_acme"
            return httpx.Response(200, json={"data": stored, "has_more": False})
        assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
        form = {key: value[0] for key, value in parse_qs(request.content.decode()).items()}
        assert form["subscription"] == "sub_test" and form["amount"] == "152" and form["currency"] == "usd"
        assert int(form["period[end]"]) == int(datetime(2025, 3, 1, tzinfo=UTC).timestamp()) - 1
        posts.append(request.headers["Idempotency-Key"])
        stored.append({"id": "ii_test", "metadata": {"sca_usage_key": form["metadata[sca_usage_key]"]}})
        raise httpx.ReadTimeout("the mock lost its response", request=request)

    settings = Settings(_env_file=None, stripe_secret_key=uuid4().hex, stripe_api_url="https://stripe.example")
    provider = StripePaymentProvider(settings, httpx.MockTransport(stripe))
    with pytest.raises(PaymentError, match="Stripe"):
        await provider.create_usage_item(item)
    assert await provider.create_usage_item(item) == "ii_test"
    assert await provider.create_usage_item(item) == "ii_test"
    assert posts == [item.key]
    assert item.key != item.model_copy(update={"month": date(2025, 3, 1)}).key


def test_admin_cli_passes_closed_month_and_markup(monkeypatch):
    settings = Settings(_env_file=None, stripe_secret_key=uuid4().hex, billing_markup=Decimal("1.5"))
    dispose = AsyncMock()
    store = SimpleNamespace(sessionmaker=SimpleNamespace(kw={"bind": SimpleNamespace(dispose=dispose)}))
    sent = AsyncMock(return_value={"month": "2025-10", "reported": 1, "failed_tenants": ["other"]})
    monkeypatch.setattr("sca.cli.get_settings", lambda: settings)
    monkeypatch.setattr("sca.db.engine.get_store", lambda: store)
    monkeypatch.setattr("sca.billing.usage_to_stripe.report_usage", sent)
    runner = CliRunner()
    result = runner.invoke(cli, ["admin", "report-usage-to-stripe", "--month", "2025-10"])
    assert result.exit_code == 1 and json.loads(result.stdout)["reported"] == 1
    assert sent.await_args.args[2:] == (date(2025, 10, 1), Decimal("1.5"))
    dispose.assert_awaited_once()
    for invalid in ("2025-1", "2025-13", "9999-12"):
        assert runner.invoke(cli, ["admin", "report-usage-to-stripe", "--month", invalid]).exit_code == 2
    sent.assert_awaited_once()
