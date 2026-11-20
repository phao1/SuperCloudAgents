from typing import Annotated, Any, Literal

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from sca.api.tenants import PlatformStore, TenantAdmin
from sca.billing.payments import PaymentError, PaymentProvider, StripePaymentProvider
from sca.billing.stripe_events import HANDLED_EVENTS, StripeEvent, verify_signature
from sca.config import Settings
from sca.db.engine import make_engine, make_sessionmaker
from sca.db.repo import Store
from sca.tenancy.models import PlanName

router = APIRouter(prefix="/v1")


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: PlanName


def _payment_provider(request: Request) -> PaymentProvider:
    provider = request.app.state.payment_provider
    if provider is None:
        raise HTTPException(status_code=503, detail="Stripe 支付尚未配置")
    return provider


@router.get("/tenant/billing")
async def tenant_billing(request: Request, response: Response, user: TenantAdmin) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    store = request.app.state.store.for_tenant(user.tenant_id)
    return {"enabled": request.app.state.payment_provider is not None,
            **await store.billing_summary()}


@router.post("/tenant/billing/checkout")
async def checkout(
    body: CheckoutRequest, request: Request, response: Response, user: TenantAdmin,
) -> dict[str, str]:
    provider = _payment_provider(request)
    store = request.app.state.store.for_tenant(user.tenant_id)
    response.headers["Cache-Control"] = "no-store"
    return {"url": await store.billing_checkout(body.plan, provider)}


@router.post("/tenant/billing/portal")
async def billing_portal(request: Request, response: Response, user: TenantAdmin) -> dict[str, str]:
    provider = _payment_provider(request)
    store = request.app.state.store.for_tenant(user.tenant_id)
    response.headers["Cache-Control"] = "no-store"
    return {"url": await store.billing_portal(provider)}


@router.get("/admin/billing")
async def admin_billing(
    store: PlatformStore, response: Response, view: Literal["subscriptions", "invoices", "events"] = "subscriptions",
    tenant_id: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    response.headers["Cache-Control"] = "no-store"
    return await store.list_platform_billing(view, tenant_id, limit, offset)


@router.post("/billing/stripe/webhook")
async def stripe_webhook(
    request: Request, stripe_signature: Annotated[str, Header()] = "",
) -> dict[str, bool]:
    provider = _payment_provider(request)
    settings: Settings = request.app.state.settings
    body = await request.body()
    assert settings.stripe_webhook_secret is not None
    if not verify_signature(body, stripe_signature, settings.stripe_webhook_secret.get_secret_value()):
        raise HTTPException(status_code=400, detail="Stripe 签名无效")
    try:
        event, payload = StripeEvent.decode(body)
        if event.type in HANDLED_EVENTS:
            await request.app.state.billing_webhook_store.apply_stripe_event(
                event, payload, provider, settings.billing_payment_failure_attempts,
            )
    except ValueError:
        raise HTTPException(status_code=400, detail="Stripe 事件格式无效") from None
    return {"received": True}


def register_payments(app: FastAPI, settings: Settings, *, default_store: bool) -> None:
    enabled = bool(settings.stripe_secret_key and settings.stripe_webhook_secret)
    app.state.payment_provider = StripePaymentProvider(settings) if enabled else None
    store = app.state.platform_store
    # Webhook lookup needs the database owner even if the platform admin API is disabled.
    if (enabled and default_store and store is app.state.store and settings.api_database_url
            and settings.api_database_url != settings.database_url):
        store = Store(make_sessionmaker(make_engine(settings.database_url)),
                      settings.default_max_active_tasks)
    app.state.billing_webhook_store = store
    app.include_router(router)

    @app.exception_handler(PaymentError)
    async def payment_error(request: Request, exc: PaymentError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "stripe_unavailable", "message": str(exc)}}, status_code=502,
        )
