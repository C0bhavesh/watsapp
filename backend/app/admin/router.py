"""Admin JSON API: login/session now; creds, knowledge, controls, views in later tasks."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.admin.auth import check_password, issue_token, verify_token
from app.deps import get_container
from app.ratelimit import limiter

logger = logging.getLogger("app.admin")

_MAX_BODY = 1_048_576  # 1 MiB — same posture as the webhook edges


class AdminBodyCapMiddleware:
    """Reject oversized ``/admin`` requests by Content-Length, before body parsing.

    A router dependency cannot enforce this: FastAPI parses (and may 422 on) the JSON
    body before path dependencies run, so an oversized *invalid* body 422s before any
    cap check. Enforcing at the ASGI layer returns 413 first, matching the webhook edges.
    """

    def __init__(self, app: ASGIApp, max_body: int = _MAX_BODY) -> None:
        self._app = app
        self._max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if isinstance(path, str) and path.startswith("/admin"):
                for name, value in scope.get("headers") or []:
                    if name == b"content-length":
                        raw = value.decode("latin-1")
                        if raw.isdigit() and int(raw) > self._max_body:
                            response = PlainTextResponse("payload too large", status_code=413)
                            await response(scope, receive, send)
                            return
                        break
        await self._app(scope, receive, send)


admin_router = APIRouter(prefix="/admin")


def _now() -> datetime:
    return datetime.now(UTC)


def require_admin(admin_session: str | None = Cookie(default=None)) -> None:
    settings = get_container().settings
    if not admin_session or not verify_token(settings.app_master_key, admin_session, _now()):
        raise HTTPException(status_code=401, detail="admin auth required")


class LoginRequest(BaseModel):
    password: str = Field(max_length=256)


@admin_router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, req: LoginRequest, response: Response) -> dict[str, bool]:
    """Verify the admin password and issue a signed session cookie on success."""
    settings = get_container().settings
    if not settings.admin_password:
        raise HTTPException(status_code=503, detail="admin not configured")
    if not check_password(req.password, settings.admin_password):
        raise HTTPException(status_code=401, detail="invalid password")
    token = issue_token(settings.app_master_key, _now())
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        "admin_session",
        token,
        httponly=True,
        samesite="strict",
        secure=forwarded_proto == "https",  # Vercel terminates TLS
        max_age=12 * 3600,  # keep in sync with issue_token ttl_hours
        path="/admin",
    )
    return {"ok": True}


@admin_router.get("/session", dependencies=[Depends(require_admin)])
async def session() -> dict[str, bool]:
    """Cookie-only auth probe — NO database access, so a DB hiccup can't log anyone out."""
    return {"ok": True}
