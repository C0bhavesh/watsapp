"""Admin JSON API: login/session now; creds, knowledge, controls, views in later tasks."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.admin.auth import check_password, issue_token, verify_token
from app.admin.knowledge_models import validate_and_serialize
from app.channels.whatsapp_config import (
    WHATSAPP_PLAIN_FIELDS,
    WHATSAPP_SECRET_FIELDS,
    load_whatsapp_config,
)
from app.deps import get_container
from app.knowledge.loader import KINDS, SEEDS_DIR, KnowledgeLoader
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


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"unknown kind: {kind}")


@admin_router.get("/knowledge/{kind}", dependencies=[Depends(require_admin)])
async def get_knowledge(kind: str) -> dict[str, str]:
    """DB override if set, else the shipped seed file."""
    _check_kind(kind)
    loader = KnowledgeLoader(get_container().config_repo, SEEDS_DIR)
    return {"kind": kind, "content": await loader.get(kind)}


@admin_router.put("/knowledge/{kind}", dependencies=[Depends(require_admin)])
async def put_knowledge(kind: str, payload: dict[str, object]) -> dict[str, bool]:
    """Validate, store the override, and bump knowledge_version (cache invalidation)."""
    _check_kind(kind)
    try:
        content = validate_and_serialize(kind, payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    repo = get_container().config_repo
    await repo.set_knowledge_override(kind, content)
    await repo.bump_config_int("knowledge_version")
    return {"ok": True}


class ShopifyCredsRequest(BaseModel):
    """Blank/omitted field = keep the stored value (first-time setup requires both)."""

    client_id: str | None = Field(default=None, max_length=256)
    client_secret: str | None = Field(default=None, max_length=256)


def _clean(v: str | None) -> str | None:
    if v is None:
        return None
    stripped = v.strip()
    return stripped if stripped else None


@admin_router.get("/shopify", dependencies=[Depends(require_admin)])
async def shopify_status() -> dict[str, bool]:
    cfg = get_container().config
    has_id = await cfg.get_secret("shopify:client_id") is not None
    has_secret = await cfg.get_secret("shopify:client_secret") is not None
    return {"configured": has_id and has_secret}


@admin_router.post("/shopify", dependencies=[Depends(require_admin)])
async def set_shopify(req: ShopifyCredsRequest) -> dict[str, bool]:
    cfg = get_container().config
    client_id, client_secret = _clean(req.client_id), _clean(req.client_secret)
    existing_id = await cfg.get_secret("shopify:client_id")
    existing_secret = await cfg.get_secret("shopify:client_secret")
    if (existing_id is None and client_id is None) or (
        existing_secret is None and client_secret is None
    ):
        raise HTTPException(
            status_code=422, detail="first-time setup requires client_id and client_secret"
        )
    if client_id is not None:
        await cfg.set_secret("shopify:client_id", client_id)
    if client_secret is not None:
        await cfg.set_secret("shopify:client_secret", client_secret)
    return {"ok": True}


_WA_ALL_FIELDS: tuple[str, ...] = WHATSAPP_SECRET_FIELDS + WHATSAPP_PLAIN_FIELDS


class WhatsAppCredsRequest(BaseModel):
    """All optional: blank/omitted keeps the stored value; first-time needs all six."""

    phone_number_id: str | None = Field(default=None, max_length=64)
    waba_id: str | None = Field(default=None, max_length=64)
    api_version: str | None = Field(default=None, max_length=16)
    access_token: str | None = Field(default=None, max_length=1024)
    app_secret: str | None = Field(default=None, max_length=256)
    verify_token: str | None = Field(default=None, max_length=256)


@admin_router.get("/whatsapp", dependencies=[Depends(require_admin)])
async def whatsapp_status() -> dict[str, object]:
    """Configured flag + NON-secret fields only."""
    cfg = await load_whatsapp_config(get_container().config)
    return {
        "configured": cfg is not None,
        "phone_number_id": cfg.phone_number_id if cfg else None,
        "waba_id": cfg.waba_id if cfg else None,
        "api_version": cfg.api_version if cfg else None,
    }


@admin_router.post("/whatsapp", dependencies=[Depends(require_admin)])
async def set_whatsapp(req: WhatsAppCredsRequest) -> dict[str, bool]:
    config = get_container().config
    values = {name: _clean(getattr(req, name)) for name in _WA_ALL_FIELDS}
    existing = await load_whatsapp_config(config)
    if existing is None:
        missing = [name for name in _WA_ALL_FIELDS if values[name] is None]
        if missing:
            raise HTTPException(
                status_code=422, detail=f"first-time setup requires: {', '.join(missing)}"
            )
    for name in WHATSAPP_SECRET_FIELDS:
        secret_value = values[name]
        if secret_value is not None:
            await config.set_secret(f"whatsapp:{name}", secret_value)
    for name in WHATSAPP_PLAIN_FIELDS:
        plain_value = values[name]
        if plain_value is not None:
            await config.set_plain(f"whatsapp:{name}", plain_value)
    return {"ok": True}
