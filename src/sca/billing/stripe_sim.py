"""Local Stripe simulator for the SCA payment adapter and signed webhook flow."""

import argparse
import asyncio
import hmac
import json
import os
import re
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from html import escape
from typing import Any
from urllib.parse import parse_qsl
from uuid import uuid4

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

DEFAULT_PORT = 12111
DEFAULT_WEBHOOK_URL = "http://127.0.0.1:8000/v1/billing/stripe/webhook"
MONTH_SECONDS = 30 * 86400
MONTHLY_AMOUNT_CENTS = 2900
type StripeObject = dict[str, Any]


def sign_webhook(payload: bytes, secret: str, *, timestamp: int | None = None) -> str:
    timestamp = int(time.time()) if timestamp is None else timestamp
    digest = hmac.digest(secret.encode(), f"{timestamp}.".encode() + payload, "sha256").hex()
    return f"t={timestamp},v1={digest}"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _event(type_: str, resource: StripeObject) -> StripeObject:
    return {"id": _id("evt"), "object": "event", "type": type_, "created": int(time.time()),
            "livemode": False, "data": {"object": deepcopy(resource)}}


def _find(resources: dict[str, StripeObject], id_: str) -> StripeObject:
    if id_ not in resources:
        raise HTTPException(404, "模拟对象不存在")
    return resources[id_]


def _http_url(value: str) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ValueError from None
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
        raise ValueError
    return str(url)


async def _error(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": {"message": exc.detail}}, status_code=exc.status_code, headers=exc.headers)


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f'<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title><main><h1>{title}</h1>{body}</main></html>"
    )


@dataclass
class _Delivery:
    action: str
    result: StripeObject
    events: list[StripeObject]


class StripeSimulator:
    def __init__(
        self, public_url: str, webhook_url: str, webhook_secret: str, *,
        webhook_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.public_url = public_url.rstrip("/")
        self.customers: dict[str, StripeObject] = {}
        self.sessions: dict[str, StripeObject] = {}
        self.portals: dict[str, StripeObject] = {}
        self.subscriptions: dict[str, StripeObject] = {}
        self.invoices: dict[str, StripeObject] = {}
        self.invoice_items: dict[str, StripeObject] = {}
        self._latest_invoice: dict[str, StripeObject] = {}
        self._idempotency: dict[str, tuple[str, dict[str, str], StripeObject]] = {}
        self._pending: dict[str, _Delivery] = {}
        self._control_lock = asyncio.Lock()
        self._webhook_url = webhook_url
        self._webhook_secret = webhook_secret
        self._webhook_transport = webhook_transport

        self.app = Starlette(routes=[
            Route("/v1/customers", self._post(self._create_customer), methods=["POST"]),
            Route("/v1/checkout/sessions", self._post(self._create_checkout), methods=["POST"]),
            Route("/v1/checkout/sessions/{id}", self._get_checkout),
            Route("/v1/checkout/sessions/{id}/expire", self._post(self._expire_checkout), methods=["POST"]),
            Route("/v1/billing_portal/sessions", self._post(self._create_portal), methods=["POST"]),
            Route("/v1/subscriptions/{id}", self._get_subscription),
            Route("/v1/invoiceitems", self._list_invoice_items),
            Route("/v1/invoiceitems", self._post(self._create_invoice_item), methods=["POST"]),
            Route("/_sim/health", _health),
            Route("/_sim/checkout/{id}", self._checkout_page, methods=["GET", "POST"]),
            Route("/_sim/checkout/{id}/complete", self._complete_checkout, methods=["POST"]),
            Route("/_sim/portal/{id}", self._portal_page),
            Route("/_sim/invoices/{id}", self._invoice_page),
            Route("/_sim/subscriptions/{id}/{action}", self._subscription_control, methods=["POST"]),
        ], exception_handlers={HTTPException: _error})

    def _post(
        self, create: Callable[[Request, dict[str, str]], StripeObject],
    ) -> Callable[[Request], Awaitable[JSONResponse]]:
        async def handle(request: Request) -> JSONResponse:
            try:
                form = dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))
                key = request.headers.get("Idempotency-Key")
                path = request.url.path
                if key and key in self._idempotency:
                    previous_path, previous_form, result = self._idempotency[key]
                    if (path, form) != (previous_path, previous_form):
                        raise HTTPException(400, "重复请求的参数不一致")
                else:
                    result = create(request, form)
                    if key:
                        self._idempotency[key] = (path, form, deepcopy(result))
            except (KeyError, ValueError):
                raise HTTPException(400, "请求参数无效") from None
            return JSONResponse(result)

        return handle

    def _create_customer(self, request: Request, form: dict[str, str]) -> StripeObject:
        customer = {"id": _id("cus"), "object": "customer",
                    "metadata": {"sca_tenant_id": form["metadata[sca_tenant_id]"]}}
        self.customers[customer["id"]] = customer
        return customer

    def _create_checkout(self, request: Request, form: dict[str, str]) -> StripeObject:
        customer = _find(self.customers, form["customer"])
        if form["mode"] != "subscription" or form["line_items[0][quantity]"] != "1":
            raise HTTPException(400, "模拟结账仅支持单项订阅")
        price_id = form["line_items[0][price]"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,255}", price_id):
            raise HTTPException(400, "套餐 Price ID 无效")
        id_ = _id("cs")
        session = {
            "id": id_, "object": "checkout.session", "mode": "subscription", "status": "open",
            "customer": customer["id"], "subscription": None, "price": price_id,
            "url": f"{self.public_url}/_sim/checkout/{id_}",
            "success_url": _http_url(form["success_url"]), "cancel_url": _http_url(form["cancel_url"]),
        }
        self.sessions[id_] = session
        return session

    def _expire_checkout(self, request: Request, form: dict[str, str]) -> StripeObject:
        session = _find(self.sessions, request.path_params["id"])
        if session["status"] != "open":
            raise HTTPException(409, "该结账会话已结束")
        session.update(status="expired", url=None)
        return session

    async def _get_checkout(self, request: Request) -> JSONResponse:
        return JSONResponse(_find(self.sessions, request.path_params["id"]))

    def _create_portal(self, request: Request, form: dict[str, str]) -> StripeObject:
        customer = _find(self.customers, form["customer"])
        id_ = _id("bps")
        portal = {"id": id_, "object": "billing_portal.session", "customer": customer["id"],
                  "return_url": _http_url(form["return_url"]), "url": f"{self.public_url}/_sim/portal/{id_}"}
        self.portals[id_] = portal
        return portal

    async def _get_subscription(self, request: Request) -> JSONResponse:
        return JSONResponse(_find(self.subscriptions, request.path_params["id"]))

    def _create_invoice_item(self, request: Request, form: dict[str, str]) -> StripeObject:
        subscription = _find(self.subscriptions, form["subscription"])
        if form["customer"] != subscription["customer"] or form["currency"] != "usd":
            raise HTTPException(400, "账单项目的客户或币种无效")
        item = {
            "id": _id("ii"), "object": "invoiceitem", "customer": form["customer"],
            "subscription": subscription["id"], "amount": int(form["amount"]), "currency": "usd",
            "description": form["description"], "created": int(time.time()), "invoice": None,
            "period": {"start": int(form["period[start]"]), "end": int(form["period[end]"])},
            "metadata": {"sca_usage_key": form["metadata[sca_usage_key]"]},
        }
        self.invoice_items[item["id"]] = item
        return item

    async def _list_invoice_items(self, request: Request) -> JSONResponse:
        params = request.query_params
        customer = params.get("customer")
        try:
            since = int(params.get("created[gte]", "0"))
            limit = int(params.get("limit", "10"))
            if not 1 <= limit <= 100:
                raise ValueError
            rows = [row for row in reversed(self.invoice_items.values())
                    if row["created"] >= since and (customer is None or row["customer"] == customer)]
            if after := params.get("starting_after"):
                index = [row["id"] for row in rows].index(after)
                rows = rows[index + 1:]
        except ValueError:
            raise HTTPException(400, "账单项目查询参数无效") from None
        return JSONResponse({"object": "list", "data": rows[:limit], "has_more": len(rows) > limit})

    async def _checkout_page(self, request: Request) -> Response:
        id_ = request.path_params["id"]
        if request.method == "POST":
            session = await self._complete(id_)
            return RedirectResponse(session["success_url"], status_code=303)
        session = _find(self.sessions, id_)
        if session["status"] == "expired":
            return _page("结账会话已过期", f'<a href="{escape(session["cancel_url"])}">返回 Portal</a>')
        return _page("Stripe 模拟结账", '<form method="post"><button>完成付款</button></form> '
                     f'<a href="{escape(session["cancel_url"])}">取消</a>')

    async def _portal_page(self, request: Request) -> HTMLResponse:
        portal = _find(self.portals, request.path_params["id"])
        links = "".join(
            f'<li><a href="{escape(invoice["hosted_invoice_url"])}">{invoice["number"]}</a></li>'
            for invoice in self.invoices.values() if invoice["customer"] == portal["customer"]
        )
        return _page("Stripe 模拟账单", (f"<h2>发票</h2><ul>{links}</ul>" if links else "<p>暂无发票</p>")
                     + f'<a href="{escape(portal["return_url"])}">返回 Portal</a>')

    async def _invoice_page(self, request: Request) -> HTMLResponse:
        invoice = _find(self.invoices, request.path_params["id"])
        status = "已付款" if invoice["status"] == "paid" else "待付款"
        return _page(f'发票 {invoice["number"]}',
                     f'<p>{status} · USD {invoice["amount_due"] / 100:.2f}</p>')

    async def _complete_checkout(self, request: Request) -> JSONResponse:
        return JSONResponse(await self._complete(request.path_params["id"]))

    async def _complete(self, id_: str) -> StripeObject:
        def simulate() -> tuple[StripeObject, list[StripeObject]]:
            session = _find(self.sessions, id_)
            if session["status"] == "expired":
                raise HTTPException(409, "该结账会话已过期")
            if session["status"] == "complete":
                return session, []
            sub_id = _id("sub")
            subscription = {
                "id": sub_id, "object": "subscription", "customer": session["customer"],
                "status": "active", "items": {"data": [{"price": {"id": session["price"]},
                                                         "current_period_end": int(time.time()) + MONTH_SECONDS}]},
            }
            self.subscriptions[sub_id] = subscription
            session.update(status="complete", subscription=sub_id, url=None)
            return session, [_event("checkout.session.completed", session),
                             _event("customer.subscription.created", subscription)]

        return await self._control(id_, "complete", simulate)

    async def _subscription_control(self, request: Request) -> JSONResponse:
        id_, action = request.path_params["id"], request.path_params["action"]
        price_id = None
        if action == "update":
            try:
                price_id = (await request.json())["price_id"]
            except (KeyError, TypeError, ValueError):
                raise HTTPException(400, "请提供套餐 Price ID") from None
            if not isinstance(price_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,255}", price_id):
                raise HTTPException(400, "套餐 Price ID 无效")
        elif action not in {"cancel", "payment_failed", "payment_succeeded"}:
            raise HTTPException(404, "模拟操作不存在")

        def simulate() -> tuple[StripeObject, list[StripeObject]]:
            subscription = _find(self.subscriptions, id_)
            if subscription["status"] == "canceled":
                if action == "cancel":
                    return subscription, []
                raise HTTPException(409, "该订阅已取消")
            if action == "cancel":
                subscription["status"] = "canceled"
                return subscription, [_event("customer.subscription.deleted", subscription)]
            if price_id is not None:
                item = subscription["items"]["data"][0]
                if price_id == item["price"]["id"]:
                    return subscription, []
                item["price"] = {"id": price_id}
                return subscription, [_event("customer.subscription.updated", subscription)]
            return self._pay_invoice(subscription, paid=action == "payment_succeeded")

        result = await self._control(id_, f"{action}:{price_id or ''}", simulate)
        return JSONResponse(result)

    def _pay_invoice(
        self, subscription: StripeObject, *, paid: bool,
    ) -> tuple[StripeObject, list[StripeObject]]:
        invoice = self._latest_invoice.get(subscription["id"])
        if invoice is None or (invoice["status"] == "paid" and not paid):
            invoice = self._new_invoice(subscription)
        if invoice["status"] == "paid":
            return invoice, []
        invoice["attempt_count"] += 1
        events = []
        status = "active" if paid else "past_due"
        if subscription["status"] != status:
            subscription["status"] = status
            events.append(_event("customer.subscription.updated", subscription))
        if paid:
            invoice.update(status="paid", amount_paid=invoice["amount_due"])
        events.append(_event("invoice.paid" if paid else "invoice.payment_failed", invoice))
        return invoice, events

    def _new_invoice(self, subscription: StripeObject) -> StripeObject:
        id_, sub_id = _id("in"), subscription["id"]
        amount = MONTHLY_AMOUNT_CENTS
        for item in self.invoice_items.values():
            if item["subscription"] == sub_id and item["invoice"] is None:
                item["invoice"] = id_
                amount += item["amount"]
        end = subscription["items"]["data"][0]["current_period_end"]
        invoice = {
            "id": id_, "object": "invoice", "customer": subscription["customer"],
            "number": f"SCA-{len(self.invoices) + 1:04d}", "status": "open", "attempt_count": 0,
            "amount_due": amount, "amount_paid": 0, "currency": "usd",
            "period_start": end - MONTH_SECONDS, "period_end": end,
            "hosted_invoice_url": f"{self.public_url}/_sim/invoices/{id_}",
            "parent": {"type": "subscription_details", "subscription_details": {"subscription": sub_id}},
        }
        self.invoices[id_] = self._latest_invoice[sub_id] = invoice
        return invoice

    async def _control(
        self, id_: str, action: str, simulate: Callable[[], tuple[StripeObject, list[StripeObject]]],
    ) -> StripeObject:
        async with self._control_lock:
            delivery = self._pending.get(id_)
            if delivery is None:
                result, events = simulate()
                delivery = self._pending[id_] = _Delivery(action, result, events)
            elif delivery.action != action:
                raise HTTPException(409, "请先重试上一次未送达的模拟操作")
            if delivery.events:
                await self._deliver(delivery.events)
            del self._pending[id_]
            return delivery.result

    async def _deliver(self, events: list[StripeObject]) -> None:
        # Keep an unacknowledged event's ID for the receiver's deduplication on retry.
        try:
            async with httpx.AsyncClient(
                transport=self._webhook_transport, timeout=10, trust_env=False,
            ) as client:
                while events:
                    payload = json.dumps(events[0], separators=(",", ":")).encode()
                    response = await client.post(self._webhook_url, content=payload, headers={
                        "Content-Type": "application/json",
                        "Stripe-Signature": sign_webhook(payload, self._webhook_secret),
                    })
                    response.raise_for_status()
                    events.pop(0)
        except httpx.HTTPError:
            raise HTTPException(502, "Stripe 模拟回调未送达，请检查 SCA 配置后重试") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--webhook-url", default=DEFAULT_WEBHOOK_URL)
    parser.add_argument("--webhook-secret", default=os.environ.get("SCA_STRIPE_WEBHOOK_SECRET"),
                        help="signing secret (defaults to SCA_STRIPE_WEBHOOK_SECRET)")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not args.webhook_secret:
        parser.error("set SCA_STRIPE_WEBHOOK_SECRET in the environment")
    try:
        webhook_url = _http_url(args.webhook_url)
    except ValueError:
        parser.error("--webhook-url requires an HTTP(S) URL without credentials")
    simulator = StripeSimulator(f"http://127.0.0.1:{args.port}", webhook_url, args.webhook_secret)
    uvicorn.run(simulator.app, host="127.0.0.1", port=args.port, workers=1, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
