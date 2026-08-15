import logging
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.admin.router import (
    AdminBodyCapMiddleware,
    AdminSecurityHeadersMiddleware,
    admin_router,
)
from app.channels.shopify_webhook import router as shopify_webhook_router
from app.channels.whatsapp import router as whatsapp_router
from app.config.settings import Settings
from app.jobs.router import router as jobs_router
from app.ratelimit import limiter


def _configure_logging() -> None:
    """Route the app's own INFO+ logs to stderr (which Vercel captures) at import time.

    Vercel invokes this module as the ASGI entrypoint (`api/index.py` -> `from app.main import
    app`), so configuring here runs on every cold start - NOT gated behind `if __name__ ==
    "__main__"`, which a serverless invocation never hits. Without this the root logger sits at
    WARNING with zero handlers, so every `logger.info(...)` in app code (including the
    owner-approved Shopify webhook debug lines) is silently discarded by `logging.lastResort`.

    Scope is deliberately the `app` namespace only, not a global `logging.basicConfig`: attaching
    the handler to the root logger would also promote third-party libraries (httpx, litellm,
    asyncpg, ...) to INFO verbosity. Idempotent - guarded so a re-import never stacks handlers.
    """
    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) for h in app_logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        app_logger.addHandler(handler)


_configure_logging()


def _docs_enabled() -> bool:
    """OpenAPI/docs are served everywhere except prod.

    Read ``app_env`` directly from Settings (not the deps container, to avoid
    building it at import time). A missing ``APP_MASTER_KEY`` means we are not in
    a configured prod deploy, so default to the dev posture (docs on).
    """
    try:
        return Settings().app_env != "prod"  # type: ignore[call-arg]
    except ValidationError:
        return True


_DOCS_ENABLED = _docs_enabled()

app = FastAPI(
    title="Thetavas Order Bot",
    docs_url="/docs" if _DOCS_ENABLED else None,
    redoc_url="/redoc" if _DOCS_ENABLED else None,
    openapi_url="/openapi.json" if _DOCS_ENABLED else None,
)

app.include_router(shopify_webhook_router)
app.include_router(whatsapp_router)
app.include_router(jobs_router)

_VALIDATION_ERROR_DROP = ("input", "url", "ctx")


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return 422 WITHOUT echoing ``input`` — over-length creds (api_key/client_secret/
    access_token/password) would otherwise be serialized verbatim into the error body
    (browser devtools/HAR, proxies, error monitors). ``ctx`` is dropped too (may hold a
    non-JSON-serializable ValueError from a custom validator); ``url`` is a noise doc link.

    FastAPI's ``RequestValidationError.errors()`` takes no kwargs, so filter the dicts by hand.
    """
    detail = [
        {k: v for k, v in err.items() if k not in _VALIDATION_ERROR_DROP} for err in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
app.add_middleware(AdminBodyCapMiddleware)
app.add_middleware(AdminSecurityHeadersMiddleware)
app.include_router(admin_router)
app.mount(
    "/admin/ui",
    StaticFiles(directory=Path(__file__).resolve().parent / "admin/static", html=True),
    name="admin-ui",
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "thetavas-order-bot"}
