import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_api import FakeOrchestrator

from sca.api.app import create_app
from sca.billing.payments import StripePaymentProvider
from sca.billing.stripe_sim import StripeSimulator, sign_webhook
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


class StripeRequests(httpx.ASGITransport):
    def __init__(self, simulator: StripeSimulator) -> None:
        super().__init__(app=simulator.app)
        self.calls: list[tuple[str, str]] = []
        self.lose_item_response = False
        self.skip_checkout_events = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        assert request.url.host == "stripe.example"
        if method == "POST":
            assert request.headers["Idempotency-Key"]
        response = await super().handle_async_request(request)
        if self.lose_item_response and (method, path) == ("POST", "/v1/invoiceitems"):
            assert response.status_code == 200
            self.lose_item_response = False
            raise httpx.ReadTimeout("test response lost", request=request)
        return response


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
    webhook_path = "/v1/billing/stripe/webhook"
    webhook_secret = settings.stripe_webhook_secret.get_secret_value()
    api_transport = httpx.ASGITransport(app=app)

    async def deliver(request: httpx.Request) -> httpx.Response:
        if stripe.skip_checkout_events and json.loads(request.content)["type"] in {
            "checkout.session.completed", "customer.subscription.created",
        }:
            return httpx.Response(200, json={"received": True})
        return await api_transport.handle_async_request(request)

    simulator = StripeSimulator(str(settings.stripe_api_url), settings.api_url + webhook_path,
                                webhook_secret, webhook_transport=httpx.MockTransport(deliver))
    stripe = StripeRequests(simulator)
    provider = StripePaymentProvider(settings, stripe)
    app.state.payment_provider = provider
    async with store._transaction() as session:
        await session.execute(text("UPDATE plans SET stripe_price_id='price_team', included_usage_usd=20 WHERE name='team'"))
        await session.execute(text("UPDATE plans SET stripe_price_id='price_enterprise' WHERE name='enterprise'"))
    async with (
        httpx.AsyncClient(transport=api_transport, base_url=settings.api_url) as client,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=simulator.app),
                          base_url=str(settings.stripe_api_url)) as sim,
    ):
        for tenant in ("acme", "other"):
            created = await client.post("/v1/admin/tenants", headers=PLATFORM,
                                        json={"id": tenant, "display_name": tenant})
            assert created.status_code == 201

        async def send(type_: str, obj: dict, *, event_id: str | None = None, valid: bool = True):
            payload = json.dumps({"id": event_id or "evt_" + uuid4().hex, "type": type_,
                                  "data": {"object": obj}}).encode()
            return await client.post(webhook_path, content=payload, headers={
                "Stripe-Signature": sign_webhook(payload, webhook_secret if valid else uuid4().hex),
            })

        yield client, sim, stripe, provider, send


async def simulate(sim: httpx.AsyncClient, path: str, *, body: dict | None = None) -> dict:
    result = await sim.post(path, json=body)
    assert result.status_code == 200
    return result.json()


async def test_checkout_webhook_plan_changes_and_recovery(billing_api):
    client, sim, stripe, _, send = billing_api

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
    old_session = first.json()["url"].rsplit("/", 1)[1]
    team = await checkout("team")
    assert team.status_code == 200
    checkout_url = httpx.URL(team.json()["url"])
    session_id = checkout_url.path.rsplit("/", 1)[1]
    assert checkout_url.host == "stripe.example" and checkout_url.path.startswith("/_sim/checkout/")
    assert (await sim.get(f"/v1/checkout/sessions/{old_session}")).json()["status"] == "expired"
    session = (await sim.get(f"/v1/checkout/sessions/{session_id}")).json()
    assert session["price"] == "price_team"
    assert session["success_url"] == session["cancel_url"] == "https://sca.example/portal#/team"
    assert (await account())["has_customer"] and (await account())["plan"] == "free"
    assert (await client.post(BILLING + "/portal", headers=OTHER)).status_code == 409
    portal = await client.post(BILLING + "/portal", headers=TENANT)
    assert portal.status_code == 200
    assert (await sim.get(portal.json()["url"])).status_code == 200

    assert (await send("checkout.session.completed", session, valid=False)).status_code == 400
    completed = await simulate(sim, f"{checkout_url.path}/complete")
    sub_id = completed["subscription"]
    subscription_url = f"/v1/subscriptions/{sub_id}"
    subscription_control = f"/_sim/subscriptions/{sub_id}"
    live = (await sim.get(subscription_url)).json()
    subscribed = await account()
    assert (subscribed["plan"], subscribed["subscription_status"]) == ("team", "active")
    assert datetime.fromisoformat(subscribed["current_period_end"]).timestamp() == (
        live["items"]["data"][0]["current_period_end"]
    )
    assert (await checkout("enterprise")).status_code == 409
    changed = await simulate(sim, f"{subscription_control}/update", body={"price_id": "price_enterprise"})
    lookups = stripe.calls.count(("GET", subscription_url))
    for _ in range(2):
        assert (await send("customer.subscription.updated", changed, event_id="evt_plan_change")).status_code == 200
    assert stripe.calls.count(("GET", subscription_url)) == lookups + 1
    assert (await send("customer.subscription.updated", live)).status_code == 200
    assert (await account())["plan"] == "enterprise"

    for attempt in range(1, 4):
        failed = await simulate(sim, f"{subscription_control}/payment_failed")
        assert failed["attempt_count"] == attempt
        assert (await account())["suspended_for_nonpayment"] == (attempt == 3)
    blocked = await client.post("/v1/tasks", headers=TENANT, json=TASK)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "tenant_suspended"
    paid = await simulate(sim, f"{subscription_control}/payment_succeeded")
    assert (await send("invoice.payment_failed", failed)).status_code == 200
    resumed = await account()
    assert not resumed["suspended_for_nonpayment"]
    [stored] = resumed["invoices"]
    assert (stored["number"], stored["status"], stored["amount_paid"]) == ("SCA-0001", "paid", 2900)
    assert stored["hosted_invoice_url"] == paid["hosted_invoice_url"]
    assert (await client.post("/v1/tasks", headers=TENANT, json=TASK)).status_code == 201

    for _ in range(3):
        manual = await simulate(sim, f"{subscription_control}/payment_failed")
    assert (await client.post("/v1/admin/tenants/acme/suspend", headers=PLATFORM)).status_code == 200
    assert (await simulate(sim, f"{subscription_control}/payment_succeeded"))["id"] == manual["id"]
    assert (await client.post("/v1/tasks", headers=TENANT, json=TASK)).status_code == 403
    assert not (await account())["suspended_for_nonpayment"]
    assert (await client.post("/v1/admin/tenants/acme/resume", headers=PLATFORM)).status_code == 200

    canceled = await simulate(sim, f"{subscription_control}/cancel")
    view = await account()
    assert (view["plan"], view["subscription_status"]) == ("free", "canceled")
    assert view["suspended_for_nonpayment"] and view["can_checkout"]
    replacement = await checkout("team")
    assert replacement.status_code == 200
    replacement_path = httpx.URL(replacement.json()["url"]).path
    stripe.skip_checkout_events = True
    new_subscription = (await simulate(sim, f"{replacement_path}/complete"))["subscription"]
    stripe.skip_checkout_events = False
    assert (await account())["subscription_status"] == "canceled"
    new_paid = await simulate(sim, f"/_sim/subscriptions/{new_subscription}/payment_succeeded")
    assert (await send("customer.subscription.deleted", canceled)).status_code == 200
    assert (await account())["plan"] == "team" and not (await account())["suspended_for_nonpayment"]

    platform = (await client.get("/v1/admin/billing", headers=PLATFORM)).json()
    assert {(row["tenant_id"], row["subscription_status"]) for row in platform} == {("acme", "active"), ("other", None)}
    receipts = (await client.get("/v1/admin/billing?view=events&tenant_id=acme", headers=PLATFORM)).json()
    assert sum(row["id"] == "evt_plan_change" for row in receipts) == 1
    assert {row["id"] for row in (await client.get("/v1/admin/billing?view=invoices&tenant_id=acme",
                                                 headers=PLATFORM)).json()} == {paid["id"], manual["id"], new_paid["id"]}


async def test_monthly_overage_is_reported_once_after_a_lost_stripe_response(billing_api, store):
    client, sim, stripe, provider, _ = billing_api
    customers = {}
    for tenant, headers in [("acme", TENANT), ("other", OTHER)]:
        response = await client.post(BILLING + "/checkout", headers=headers, json={"plan": "team"})
        assert response.status_code == 200
        path = httpx.URL(response.json()["url"]).path
        customers[tenant] = (await simulate(sim, f"{path}/complete"))["customer"]
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
    items = (await sim.get("/v1/invoiceitems")).json()["data"]
    assert [(item["customer"], item["amount"]) for item in items] == [(customers["acme"], 152)]


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
