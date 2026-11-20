import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException

from sca.api.auth import (
    ChainAuthenticator,
    DatabaseTokenAuthenticator,
    OidcAuthenticator,
    StaticTokenAuthenticator,
)
from sca.api.authz import ForbiddenError
from sca.api.routes import router
from sca.api.tasks_admission import router as admission_router
from sca.billing.quota import PlanQuotaExceededError
from sca.config import get_settings
from sca.core.errors import AuthError, ConflictError, InvalidTaskError, NotFoundError
from sca.core.interfaces import Authenticator, ObjectStore, Orchestrator
from sca.db.engine import EventListener, make_engine, make_sessionmaker
from sca.db.repo import QuotaExceededError, Store
from sca.observability import Metrics, instrument_app, setup_tracing
from sca.storage import make_object_store
from sca.tenancy.models import TenantSuspendedError


def create_app(
    *,
    store: Store | None = None,
    orchestrator: Orchestrator | None = None,
    authenticator: Authenticator | None = None,
    object_store: ObjectStore | None = None,
    platform_store: Store | None = None,
) -> FastAPI:
    settings = get_settings()
    setup_tracing("sca-api", settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.orchestrator is None:
            from sca.orchestration.client import TemporalOrchestrator

            app.state.orchestrator = await TemporalOrchestrator.connect(settings)
        listener_task = asyncio.create_task(app.state.event_listener.run())
        try:
            yield
        finally:
            listener_task.cancel()
            with suppress(asyncio.CancelledError):
                await listener_task

    app = FastAPI(lifespan=lifespan)
    app.state.store = (
        store
        if store is not None
        else Store(
            make_sessionmaker(
                make_engine(getattr(settings, "api_database_url", None) or settings.database_url)
            ),
            getattr(settings, "default_max_active_tasks", 20),
        )
    )
    app.state.event_listener = EventListener(
        app.state.store.sessionmaker.kw["bind"].url.render_as_string(hide_password=False)
    )
    app.state.metrics = Metrics()
    instrument_app(app, app.state.metrics)
    app.state.orchestrator = orchestrator
    authenticators: list[Authenticator] = [
        StaticTokenAuthenticator(settings.token_table(), app.state.store),
        DatabaseTokenAuthenticator(app.state.store),
    ]
    issuer = getattr(settings, "oidc_issuer", None)
    if issuer:
        audience = getattr(settings, "oidc_audience", None)
        if not audience:
            raise ValueError("OIDC audience is required")
        authenticators.append(
            OidcAuthenticator(
                issuer,
                audience,
                getattr(settings, "oidc_jwks_url", None) or issuer + "/keys",
                getattr(settings, "oidc_tenant_claim", "tenant"),
                settings.oidc_role_claim,
                settings.oidc_default_role,
                store=app.state.store,
            )
        )
    app.state.authenticator = authenticator or ChainAuthenticator(authenticators)
    app.state.object_store = object_store or make_object_store(settings)
    app.state.store.objects = app.state.object_store

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        from prometheus_client import CONTENT_TYPE_LATEST

        return Response(
            await app.state.metrics.render(app.state.store),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    async def domain_error(request: Request, exc: Exception) -> JSONResponse:
        status, code = {
            QuotaExceededError: (429, "quota_exceeded"),
            PlanQuotaExceededError: (402, "quota_exceeded"),
            TenantSuspendedError: (403, "tenant_suspended"),
            AuthError: (401, "unauthorized"),
            ForbiddenError: (403, "forbidden"),
            NotFoundError: (404, "not_found"),
            ConflictError: (409, "conflict"),
            InvalidTaskError: (422, "validation_error"),
        }[type(exc)]
        return JSONResponse({"error": {"code": code, "message": str(exc)}}, status_code=status)

    for error in (
        ForbiddenError,
        QuotaExceededError,
        PlanQuotaExceededError,
        TenantSuspendedError,
        AuthError,
        NotFoundError,
        ConflictError,
        InvalidTaskError,
    ):
        app.add_exception_handler(error, domain_error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "validation_error", "message": str(exc)}}, status_code=422
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "http_error", "message": str(exc.detail)}},
            status_code=exc.status_code,
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    from sca.api.evals import router as evals_router
    from sca.api.evolutions import router as evolutions_router
    from sca.api.files import router as files_router
    from sca.api.memories import router as memories_router
    from sca.api.portal import router as portal_router

    @app.get("/portal", include_in_schema=False)
    async def portal() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "portal.html")

    @app.get("/portal/architecture", include_in_schema=False)
    async def portal_architecture() -> FileResponse:
        bundled = Path(__file__).parent / "static" / "architecture.html"
        source = Path(__file__).resolve().parents[3] / "Milestone" / "index.html"
        return FileResponse(bundled if bundled.is_file() else source)

    @app.get("/portal/task-flow.js", include_in_schema=False)
    async def portal_task_flow() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "task-flow.js",
                            media_type="text/javascript")

    @app.get("/portal/task-timeline.js", include_in_schema=False)
    async def portal_task_timeline() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "task-timeline.js",
                            media_type="text/javascript")

    try:
        from sca.api.agents import router as agents_router
    except ImportError:
        pass
    else:
        app.include_router(agents_router)

    app.include_router(evals_router)
    app.include_router(evolutions_router)
    app.include_router(portal_router)
    app.include_router(memories_router)
    app.include_router(files_router)
    app.include_router(admission_router)
    task_routes = APIRouter()
    task_routes.routes.extend(
        route for route in router.routes
        if not (getattr(route, "path", None) == "/v1/tasks"
                and "POST" in getattr(route, "methods", set()))
    )
    app.include_router(task_routes)

    # Tenant administration and billing
    from sca.api.tenants import router as tenancy_router
    from sca.api.usage import router as usage_router

    app.state.check_tenant_submission = app.state.store.check_tenant_submission
    app.state.platform_admin_token = settings.platform_admin_token
    if platform_store is None and store is None and settings.platform_admin_token and (
        settings.api_database_url and settings.api_database_url != settings.database_url
    ):
        platform_store = Store(make_sessionmaker(make_engine(settings.database_url)),
                               settings.default_max_active_tasks)
    app.state.platform_store = platform_store or app.state.store
    app.include_router(tenancy_router)
    app.include_router(usage_router)
    return app
