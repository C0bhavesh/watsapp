import hmac
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.deps import Container, get_container
from app.shopify.errors import ShopifyError
from app.shopify.subscriptions import ensure_subscription

router = APIRouter()

JobFn = Callable[[Container], Awaitable[dict[str, Any]]]


async def _job_ensure_subscription(c: Container) -> dict[str, Any]:
    base_url = await c.config.get_plain("public_base_url")
    if not base_url:
        return {"error": "public_base_url not configured"}
    status = await ensure_subscription(c.shopify, f"{base_url.rstrip('/')}/webhooks/shopify")
    return {"status": status}


JOBS: dict[str, JobFn] = {
    "ensure_subscription": _job_ensure_subscription,
}


MIN_CRON_SECRET_LEN = 16


@router.api_route("/internal/jobs/{name}", methods=["POST"])
async def run_job(name: str, request: Request) -> JSONResponse:
    c = get_container()
    secret = c.settings.cron_secret
    if not secret or len(secret) < MIN_CRON_SECRET_LEN:
        return JSONResponse({"error": "jobs disabled"}, status_code=503)
    provided = request.headers.get("X-Cron-Secret", "")
    try:
        provided_bytes = provided.encode("ascii")
    except UnicodeEncodeError:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not hmac.compare_digest(secret.encode("ascii"), provided_bytes):
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
