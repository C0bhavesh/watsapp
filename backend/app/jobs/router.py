import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.deps import Container, get_container
from app.jobs.delivery_confirm import run_delivery_confirm
from app.jobs.outbox_drain import run_outbox_drain
from app.jobs.reconcile import run_reconcile_cancels
from app.jobs.reminders import run_send_reminders
from app.jobs.retention import run_retention_purge
from app.shopify.errors import ShopifyError

router = APIRouter()

JobFn = Callable[[Container], Awaitable[dict[str, Any]]]


JOBS: dict[str, JobFn] = {
    "retention_purge": run_retention_purge,
    "outbox_drain": run_outbox_drain,
    "reconcile_cancels": run_reconcile_cancels,
    "send_reminders": run_send_reminders,
    "delivery_confirm": run_delivery_confirm,
}


MIN_CRON_SECRET_LEN = 16
_BEARER_PREFIX = "Bearer "


def _secret_matches(secret: str, provided: str) -> bool:
    """Constant-time compare of a header-derived candidate against the configured secret.

    Both sides are encoded to ASCII bytes first; a non-ASCII candidate (Starlette decodes headers
    latin-1) fails closed rather than raising. Same idiom as ``channels/shopify_signature.py`` and
    ``admin/auth.py`` — never ``compare_digest`` on two raw ``str``.
    """
    try:
        provided_bytes = provided.encode("ascii")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(secret.encode("ascii"), provided_bytes)


def _provided_secret(request: Request) -> str:
    """Pull the cron secret from the header appropriate to the method.

    Vercel Cron fires ``GET`` and injects ``Authorization: Bearer <CRON_SECRET>``. A manual/admin
    ``POST`` uses the ``X-Cron-Secret`` header. Each method reads ONLY its own header, so a GET
    carrying the wrong header (``X-Cron-Secret`` but no ``Authorization``) is not accidentally
    accepted, and vice versa.
    """
    if request.method == "GET":
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith(_BEARER_PREFIX):
            return ""
        return authorization[len(_BEARER_PREFIX):]
    return request.headers.get("X-Cron-Secret", "")


@router.api_route("/internal/jobs/{name}", methods=["GET", "POST"])
async def run_job(name: str, request: Request) -> JSONResponse:
    c = get_container()
    secret = c.settings.cron_secret
    if not secret or len(secret) < MIN_CRON_SECRET_LEN:
        return JSONResponse({"error": "jobs disabled"}, status_code=503)
    if not _secret_matches(secret, _provided_secret(request)):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    job = JOBS.get(name)
    if job is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    # A Shopify-layer failure is an upstream (502) condition, not a bug in this service.
    # Never echo the exception text — it can carry vendor detail. Other errors propagate.
    try:
        result = await job(c)
    except ShopifyError:
        return JSONResponse({"job": name, "error": "job failed"}, status_code=502)
    return JSONResponse({"job": name, "result": result})
