import asyncio
import hmac
import json
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_api import FakeOrchestrator

from sca.api.app import create_app
from sca.billing.payments import StripePaymentProvider
from sca.billing.usage_to_stripe import report_usage
from sca.config import Settings
from sca.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration
PLATFORM = {"Authorization": "Bearer platform-fixture"}
TENANT = {"Authorization": "Bearer tenant-fixture"}
OTHER = {"Authorization": "Bearer other-fixture"}
VIEWER = {"Authorization": "Bearer viewer-fixture"}
BILLING = "/v1/tenant/billing"
TASK = {"kind": "coding", "spec": {"repo_url": "/tmp/repo", "instruction": "fix"}}
PERIOD_END = 1796083200


class FakeStripe:
    def __init__(self):
        self.calls = []
        self.customers = {}
        self.sessions = {}
        self.subscriptions = {}
        self.invoice_items = []
        self.lose_item_response = False

    def request(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        assert request.url.host == "stripe.example"
        if method == "POST":
            assert request.headers["Idempotency-Key"]
        form = {key: value[0] for key, value in parse_qs(request.content.decode()).items()}
        if path == "/v1/customers":
            tenant = form["metadata[sca_tenant_id]"]
            assert tenant not in self.customers
            self.customers[tenant] = "cus_" + tenant
            result = {"id": self.customers[tenant]}
        elif path == "/v1/checkout/sessions":
            customer = form["customer"]
            assert customer in self.customers.values()
            assert form["mode"] == "subscription" and form["line_items[0][quantity]"] == "1"
            assert form["success_url"] == form["cancel_url"] == "https://sca.example/portal#/team"
            session_id = "cs_" + str(len(self.sessions) + 1)
            result = self.sessions[session_id] = {
                "id": session_id, "status": "open", "customer": customer,
                "url": "https://checkout.example/" + session_id,
                "price": form["line_items[0][price]"],
            }
        elif path.startswith("/v1/checkout/sessions/"):
            parts = path.split("/")
            result = self.sessions[parts[4]]
            if method == "POST":
                assert parts[-1] == "expire"
                result.update(status="expired", url=None)
        elif path == "/v1/billing_portal/sessions":
            assert form["customer"] in self.customers.values()
            result = {"url": "https://billing.example/" + form["customer"]}
        elif path.startswith("/v1/subscriptions/"):
            result = self.subscriptions[path.rsplit("/", 1)[1]]
        elif path == "/v1/invoiceitems" and method == "GET":
            result = {"has_more": False, "data": [
                row for row in self.invoice_items if row["customer"] == request.url.params["customer"]
            ]}
        elif path == "/v1/invoiceitems" and method == "POST":
            assert form["currency"] == "usd"
            assert form["metadata[sca_usage_key]"] == request.headers["Idempotency-Key"]
            subscription = self.subscriptions[form["subscription"]]
            assert form["customer"] == subscription["customer"]
            result = {"id": "ii_" + str(len(self.invoice_items) + 1), "customer": form["customer"],
                      "amount": int(form["amount"]), "metadata": {"sca_usage_key": request.headers["Idempotency-Key"]}}
            self.invoice_items.append(result)
            if self.lose_item_response:
                self.lose_item_response = False
                raise httpx.ReadTimeout("test response lost", request=request)
        else:
            raise AssertionError((method, path))
        return httpx.Response(200, json=result)

    def subscribe(self, tenant: str, subscription_id: str, price: str, status: str = "active") -> dict:
        subscription = {
            "id": subscription_id, "customer": self.customers[tenant], "status": status,
            "items": {"data": [{"price": {"id": price}, "current_period_end": PERIOD_END}]},
        }
        self.subscriptions[subscription_id] = subscription
        return subscription


@pytest.fixture
async def billing_api(store, api_store, tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None, data_dir=tmp_path, api_url="https://sca.example",
        api_tokens="tenant-fixture:acme:admin,other-fixture:other:admin,viewer-fixture:acme:viewer",
        platform_admin_token="platform-fixture", stripe_secret_key=uuid4().hex,
        stripe_webhook_secret=uuid4().hex, stripe_api_url="https://stripe.example",
        billing_markup=Decimal("1.5"),
    )
    monkeypatch.setattr("sca.config.get_settings", lambda: settings)
    monkeypatch.setattr("sca.api.app.get_settings", lambda: settings)
    objects = LocalObjectStore(tmp_path)
    app = create_app(store=api_store, platform_store=store, object_store=objects,
                     orchestrator=FakeOrchestrator(store))
    fake = FakeStripe()
    provider = StripePaymentProvider(settings, httpx.MockTransport(fake.request))
    app.state.payment_provider = provider
    async with store._transaction() as session:
        await session.execute(text("UPDATE plans SET stripe_price_id='price_team', included_usage_usd=20 WHERE name='team'"))
        await session.execute(text("UPDATE plans SET stripe_price_id='price_enterprise' WHERE name='enterprise'"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for tenant in ("acme", "other"):
            created = await client.post("/v1/admin/tenants", headers=PLATFORM,
                                        json={"id": tenant, "display_name": tenant})
            assert created.status_code == 201

        async def send(type_: str, obj: dict, *, event_id: str | None = None, valid: bool = True):
            payload = json.dumps({"id": event_id or "evt_" + uuid4().hex, "type": type_,
                                  "data": {"object": obj}}).encode()
            timestamp = int(time.time())
            secret = settings.stripe_webhook_secret.get_secret_value().encode()
            digest = hmac.digest(secret, str(timestamp).encode() + b"." + payload, "sha256").hex()
            return await client.post("/v1/billing/stripe/webhook", content=payload, headers={
                "Stripe-Signature": f"t={timestamp},v1={digest if valid else 'bad'}",
            })

        yield client, fake, provider, send


def invoice(customer: str, subscription: str, invoice_id: str, attempt: int, *, paid: bool = False):
    return {
        "id": invoice_id, "customer": customer, "number": "SCA-0001", "status": "paid" if paid else "open",
        "attempt_count": attempt, "amount_due": 2900, "amount_paid": 2900 if paid else 0, "currency": "usd",
        "period_start": PERIOD_END - 86400 * 30, "period_end": PERIOD_END,
        "hosted_invoice_url": "https://invoice.example/" + invoice_id,
        "parent": {"type": "subscription_details", "subscription_details": {"subscription": subscription}},
    }


async def test_checkout_webhook_plan_changes_and_recovery(billing_api):
    client, stripe, _, send = billing_api

    async def account():
        result = await client.get(BILLING, headers=TENANT)
        assert result.status_code == 200
        return result.json()

    async def checkout(plan):
        return await client.post(BILLING + "/checkout", headers=TENANT, json={"plan": plan})

    assert (await account())["plan"] == "free"
    assert (await client.get(BILLING, headers=VIEWER)).status_code == 403
    assert (await client.get("/v1/admin/billing", headers=TENANT)).status_code == 403
    assert (await checkout("free")).status_code == 409
    assert (await checkout("unknown")).status_code == 404
    assert stripe.calls == []
    first, retry = await asyncio.gather(checkout("enterprise"), checkout("enterprise"))
    assert first.status_code == retry.status_code == 200 and first.json() == retry.json()
    assert stripe.calls.count(("POST", "/v1/customers")) == 1
    assert stripe.calls.count(("POST", "/v1/checkout/sessions")) == 1
    assert (await checkout("team")).json() == {"url": "https://checkout.example/cs_2"}
    assert stripe.sessions["cs_1"]["status"] == "expired"
    assert stripe.sessions["cs_2"]["price"] == "price_team"
    assert (await account())["has_customer"] and (await account())["plan"] == "free"
    assert (await client.post(BILLING + "/portal", headers=OTHER)).status_code == 409
    assert (await client.post(BILLING + "/portal", headers=TENANT)).json() == {
        "url": "https://billing.example/cus_acme",
    }

    live = stripe.subscribe("acme", "sub_acme", "price_team")
    stripe.sessions["cs_2"].update(status="complete", subscription="sub_acme", url=None)
    completed = {"id": "cs_2", "mode": "subscription", "customer": "cus_acme", "subscription": "sub_acme"}
    assert (await send("checkout.session.completed", completed, valid=False)).status_code == 400
    assert (await send("checkout.session.completed", completed)).status_code == 200
    subscribed = await account()
    assert (subscribed["plan"], subscribed["subscription_status"]) == ("team", "active")
    assert datetime.fromisoformat(subscribed["current_period_end"]) == datetime.fromtimestamp(PERIOD_END, UTC)
    assert (await checkout("enterprise")).status_code == 409
    assert (await send("customer.subscription.created", live)).status_code == 200
    changed = stripe.subscribe("acme", "sub_acme", "price_enterprise")
    for _ in range(2):
        assert (await send("customer.subscription.updated", changed, event_id="evt_plan_change")).status_code == 200
    assert stripe.calls.count(("GET", "/v1/subscriptions/sub_acme")) == 3
    assert (await send("customer.subscription.updated", live)).status_code == 200
    assert (await account())["plan"] == "enterprise"

    for attempt in range(1, 4):
        failed = invoice("cus_acme", "sub_acme", "in_acme", attempt)
        assert (await send("invoice.payment_failed", failed)).status_code == 200
        assert (await account())["suspended_for_nonpayment"] == (attempt == 3)
    blocked = await client.post("/v1/tasks", headers=TENANT, json=TASK)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "tenant_suspended"
    paid = invoice("cus_acme", "sub_acme", "in_acme", 4, paid=True)
    assert (await send("invoice.paid", paid)).status_code == 200
    assert (await send("invoice.payment_failed", failed)).status_code == 200
    resumed = await account()
    assert not resumed["suspended_for_nonpayment"]
    [stored] = resumed["invoices"]
    assert (stored["number"], stored["status"], stored["amount_paid"]) == ("SCA-0001", "paid", 2900)
    assert stored["hosted_invoice_url"] == paid["hosted_invoice_url"]
    assert (await client.post("/v1/tasks", headers=TENANT, json=TASK)).status_code == 201

    assert (await send("invoice.payment_failed", invoice("cus_acme", "sub_acme", "in_manual", 3))).status_code == 200
    assert (await client.post("/v1/admin/tenants/acme/suspend", headers=PLATFORM)).status_code == 200
    assert (await send("invoice.paid", invoice("cus_acme", "sub_acme", "in_manual", 4, paid=True))).status_code == 200
    assert (await client.post("/v1/tasks", headers=TENANT, json=TASK)).status_code == 403
    assert not (await account())["suspended_for_nonpayment"]
    assert (await client.post("/v1/admin/tenants/acme/resume", headers=PLATFORM)).status_code == 200

    canceled = stripe.subscribe("acme", "sub_acme", "price_enterprise", "canceled")
    assert (await send("customer.subscription.deleted", canceled)).status_code == 200
    view = await account()
    assert (view["plan"], view["subscription_status"]) == ("free", "canceled")
    assert view["suspended_for_nonpayment"] and view["can_checkout"]
    assert (await checkout("team")).status_code == 200
    stripe.subscribe("acme", "sub_new", "price_team")
    assert (await send("invoice.paid", invoice("cus_acme", "sub_new", "in_new", 1, paid=True))).status_code == 200
    assert (await send("customer.subscription.deleted", canceled)).status_code == 200
    assert (await account())["plan"] == "team" and not (await account())["suspended_for_nonpayment"]

    platform = (await client.get("/v1/admin/billing", headers=PLATFORM)).json()
    assert {(row["tenant_id"], row["subscription_status"]) for row in platform} == {("acme", "active"), ("other", None)}
    receipts = (await client.get("/v1/admin/billing?view=events&tenant_id=acme", headers=PLATFORM)).json()
    assert sum(row["id"] == "evt_plan_change" for row in receipts) == 1
    assert {row["id"] for row in (await client.get("/v1/admin/billing?view=invoices&tenant_id=acme",
                                                 headers=PLATFORM)).json()} == {"in_acme", "in_manual", "in_new"}


async def test_monthly_overage_is_reported_once_after_a_lost_stripe_response(billing_api, store):
    client, stripe, provider, send = billing_api
    for tenant, headers in [("acme", TENANT), ("other", OTHER)]:
        assert (await client.post(BILLING + "/checkout", headers=headers, json={"plan": "team"})).status_code == 200
        sub = stripe.subscribe(tenant, "sub_" + tenant, "price_team")
        assert (await send("customer.subscription.created", sub)).status_code == 200
    async with store._transaction() as session:
        await session.execute(text("""INSERT INTO usage_daily (tenant_id, day, cost_usd) VALUES
            ('acme', '2025-10-01', 19), ('acme', '2025-10-31', 2.01), ('acme', '2025-11-01', 10),
            ('other', '2025-10-31', 20)"""))
    month = date(2025, 10, 1)
    stripe.lose_item_response = True
    assert await report_usage(store, provider, month, Decimal("1.5")) == {
        "month": "2025-10", "reported": 0, "failed_tenants": ["acme"],
    }
    async with store._transaction() as session:
        await session.execute(text("UPDATE usage_daily SET cost_usd=50 WHERE tenant_id='acme'"))
    second, concurrent = await asyncio.gather(*(report_usage(store, provider, month, Decimal("1.5")) for _ in range(2)))
    assert not second["failed_tenants"] and not concurrent["failed_tenants"]
    assert second["reported"] + concurrent["reported"] == 1
    assert (await report_usage(store, provider, month, Decimal(2)))["reported"] == 0
    assert stripe.calls.count(("POST", "/v1/invoiceitems")) == 1
    assert [(item["customer"], item["amount"]) for item in stripe.invoice_items] == [("cus_acme", 152)]


async def test_payment_tables_enforce_rls(store, api_store):
    for tenant in ("acme", "other", "unbilled"):
        await store.create_tenant(tenant, tenant, "free", None)
    async with store._transaction() as session:
        await session.execute(text("""INSERT INTO billing_customers (tenant_id, stripe_customer_id)
            VALUES ('acme', 'cus_acme'), ('other', 'cus_other')"""))
        await session.execute(text("""INSERT INTO billing_events (id, type, tenant_id, payload)
            VALUES ('evt_acme', 'invoice.paid', 'acme', '{}'), ('evt_other', 'invoice.paid', 'other', '{}')"""))
        await session.execute(text("""INSERT INTO billing_invoices (id, tenant_id, amount_due, amount_paid,
            currency, status, attempt_count) VALUES ('in_acme', 'acme', 1, 1, 'usd', 'paid', 1),
            ('in_other', 'other', 1, 1, 'usd', 'paid', 1)"""))
        await session.execute(text("""INSERT INTO billing_usage_reports (tenant_id, month, stripe_customer_id,
            subscription_id, amount_cents) VALUES ('acme', '2025-10-01', 'cus_acme', 'sub_acme', 1),
            ('other', '2025-10-01', 'cus_other', 'sub_other', 1)"""))
    for reader, expected in [(api_store, []), (api_store.for_tenant("acme"), ["acme"]),
                             (api_store.for_tenant("other"), ["other"])]:
        async with reader._transaction() as session:
            for table in ("billing_customers", "billing_events", "billing_invoices", "billing_usage_reports"):
                assert list(await session.scalars(text(f"SELECT tenant_id FROM {table}"))) == expected
    async with api_store.for_tenant("acme")._transaction() as session:
        result = await session.execute(text("UPDATE billing_customers SET checkout_price_id='x' WHERE tenant_id='other'"))
        assert result.rowcount == 0
    for sql in (
        "INSERT INTO billing_customers (tenant_id, stripe_customer_id) VALUES ('unbilled', 'cus_unbilled')",
        "UPDATE billing_customers SET subscription_status='active' WHERE tenant_id='acme'",
        "SELECT payload FROM billing_events WHERE tenant_id='acme'",
    ):
        with pytest.raises(DBAPIError):
            async with api_store.for_tenant("acme")._transaction() as session:
                await session.execute(text(sql))
