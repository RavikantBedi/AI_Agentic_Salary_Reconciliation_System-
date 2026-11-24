"""FastAPI application: middleware, templates, error handling, routing."""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime

from fastapi import FastAPI, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, settings
from .db import SessionLocal, init_db
from .deps import NotAuthenticated, base_context
from .enums import (
    PROVENANCE_LABELS,
    VerificationLevel,
    confidence_band,
)
from .i18n import health_label, outcome_label, t
from .money import format_money
from .security import PermissionDenied, rate_limiter

# Windows consoles default to cp1252, which cannot print Devanagari. Force
# UTF-8 so Hindi log output does not crash the server.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - platform dependent
        pass

logging.basicConfig(
    level=logging.INFO if not settings.debug else logging.DEBUG,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("srs")

app = FastAPI(
    title=settings.app_name,
    description="AI-agentic salary, honorarium and government payment reconciliation.",
    version="1.0.0",
    docs_url="/api/docs" if settings.debug else None,
    redoc_url=None,
    # The schema enumerates every route and parameter. That is useful in
    # development and needless exposure in production, so it is withdrawn
    # along with the docs page rather than left reachable on its own.
    openapi_url="/openapi.json" if settings.debug else None,
)

class _Templates(Jinja2Templates):
    """Jinja2Templates that accepts the ``(name, context)`` call style.

    Starlette now wants ``TemplateResponse(request, name, context)``. Every
    route in this application builds a context dict that already carries the
    request, so the shim reorders the arguments in one place rather than
    forcing the same edit through several dozen handlers.
    """

    def TemplateResponse(self, *args, **kwargs):  # noqa: N802 - matches Starlette
        if args and isinstance(args[0], str):
            name = args[0]
            context = args[1] if len(args) > 1 else (kwargs.pop("context", None) or {})
            request = context.get("request")
            return super().TemplateResponse(request, name, context, *args[2:], **kwargs)
        return super().TemplateResponse(*args, **kwargs)


templates = _Templates(directory=str(BASE_DIR / "app" / "web" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "web" / "static")), name="static")


# ---------------------------------------------------------------------------
# Template filters and globals
# ---------------------------------------------------------------------------
def _money(paise, currency: str = "INR") -> str:
    return format_money(paise, currency)


def _date(value, fmt: str = "%d %b %Y") -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime(fmt)
    if isinstance(value, date):
        return value.strftime(fmt)
    return str(value)


def _datetime(value, fmt: str = "%d %b %Y, %H:%M") -> str:
    return _date(value, fmt)


def _severity_class(severity: str) -> str:
    return {
        "CRITICAL": "sev-critical", "HIGH": "sev-high",
        "MEDIUM": "sev-medium", "LOW": "sev-low",
    }.get(severity, "sev-low")


def _outcome_class(outcome: str) -> str:
    good = {"FULLY_RECONCILED"}
    neutral = {"AWAITING_CONFIRMATION", "AVAILABLE"}
    if outcome in good:
        return "state-ok"
    if outcome in neutral:
        return "state-neutral"
    return "state-warn"


def _status_symbol(outcome: str) -> str:
    """Text symbol so status never depends on colour alone (spec s61)."""
    if outcome in {"FULLY_RECONCILED", "VERIFIED", "RESOLVED", "CONFIRMED", "MATCHED"}:
        return "✓"   # check
    if outcome in {"AWAITING_CONFIRMATION", "IMPORTED", "DRAFT", "SUGGESTED"}:
        return "•"   # bullet
    if outcome in {"NOT_PAID", "PAYMENT_REVERSED", "REJECTED", "UNRESOLVED"}:
        return "✕"   # cross
    return "⚠"       # warning


def _percent(value, digits: int = 0) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.{digits}f}%" if value <= 1 else f"{float(value):.{digits}f}%"


templates.env.filters.update({
    "money": _money,
    "date": _date,
    "datetime": _datetime,
    "severity_class": _severity_class,
    "outcome_class": _outcome_class,
    "status_symbol": _status_symbol,
    "percent": _percent,
    "confidence_band": confidence_band,
})
templates.env.globals.update({
    "app_name": settings.app_name,
    "verification_label": VerificationLevel.label,
    "outcome_label": outcome_label,
    "health_label": health_label,
    "provenance_labels": PROVENANCE_LABELS,
    "translate": t,
})


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def security_and_limits(request: Request, call_next):
    """Rate limiting plus baseline security headers (spec s50)."""
    path = request.url.path

    if not path.startswith("/static"):
        client = request.client.host if request.client else "unknown"
        if path == "/login" and request.method == "POST":
            key, limit = f"login:{client}", settings.rate_limit_login_max
        elif request.method == "POST" and ("upload" in path or "import" in path):
            key, limit = f"upload:{client}", settings.rate_limit_upload_max
        else:
            key, limit = f"req:{client}", settings.rate_limit_max_requests

        if not rate_limiter.allow(key, limit, settings.rate_limit_window_seconds):
            return PlainTextResponse(
                "Too many requests. Please wait a moment and try again.",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )

    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    # No external resources are used, so the policy can be strict.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; form-action 'self'; base-uri 'self'; "
        "frame-ancestors 'none'; object-src 'none'"
    )
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
def _wants_json(request: Request) -> bool:
    return (request.url.path.startswith("/api")
            or "application/json" in (request.headers.get("accept") or ""))


@app.exception_handler(NotAuthenticated)
async def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    if _wants_json(request):
        return JSONResponse({"error": "Not signed in."}, status_code=401)
    nxt = request.url.path
    return RedirectResponse(url=f"/login?next={nxt}", status_code=status.HTTP_303_SEE_OTHER)


@app.exception_handler(PermissionDenied)
async def handle_permission_denied(request: Request, exc: PermissionDenied):
    message = str(exc) or "You do not have access to this page."
    logger.warning("Permission denied on %s: %s", request.url.path, message)
    if _wants_json(request):
        return JSONResponse({"error": message}, status_code=403)

    db = SessionLocal()
    try:
        from .deps import get_optional_user

        user = get_optional_user(request, db)
        context = base_context(request, db, user)
        context.update({"title": "Access denied", "message": message, "code": 403})
        return templates.TemplateResponse("error.html", context, status_code=403)
    finally:
        db.close()


@app.exception_handler(404)
async def handle_404(request: Request, exc):
    if _wants_json(request):
        return JSONResponse({"error": "Not found."}, status_code=404)
    db = SessionLocal()
    try:
        from .deps import get_optional_user

        user = get_optional_user(request, db)
        context = base_context(request, db, user)
        context.update({
            "title": "Page not found",
            "message": "The page you were looking for does not exist.",
            "code": 404,
        })
        return templates.TemplateResponse("error.html", context, status_code=404)
    finally:
        db.close()


@app.exception_handler(500)
async def handle_500(request: Request, exc):
    logger.exception("Unhandled error on %s", request.url.path)
    if _wants_json(request):
        return JSONResponse({"error": "An internal error occurred."}, status_code=500)
    db = SessionLocal()
    try:
        from .deps import get_optional_user

        user = get_optional_user(request, db)
        context = base_context(request, db, user)
        context.update({
            "title": "Something went wrong",
            "message": ("An unexpected error occurred. Your data has not been changed. "
                        "Please try again, or contact your administrator if it continues."),
            "code": 500,
        })
        return templates.TemplateResponse("error.html", context, status_code=500)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup() -> None:
    init_db()
    from .services.jobs import start_workers

    start_workers()
    logger.info("%s ready. Database: %s", settings.app_name,
                settings.database_url.split("///")[-1])
    if not settings.llm_available:
        logger.info("AI narrative layer is OFF (no ANTHROPIC_API_KEY). "
                    "All calculations run on the deterministic engines.")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
from .routers import (  # noqa: E402
    admin,
    assistant,
    auth,
    dashboard,
    disputes,
    documents,
    imports,
    reports,
    transactions,
)

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(transactions.router)
app.include_router(imports.router)
app.include_router(documents.router)
app.include_router(disputes.router)
app.include_router(reports.router)
app.include_router(assistant.router)
app.include_router(admin.router)
