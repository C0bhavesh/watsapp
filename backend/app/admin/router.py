"""Admin JSON API: login/session now; creds, knowledge, controls, views in later tasks."""

import hashlib
import hmac
import json
import logging
import re
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, StringConstraints, ValidationError
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.admin.auth import check_password, issue_token, verify_token
from app.admin.controls import AdminControls, load_controls, save_controls
from app.admin.knowledge_models import validate_and_serialize
from app.admin.template_catalog import TEMPLATE_CATALOG, resolve_template_defaults
from app.channels.copy import EMPTY_PARAM_PLACEHOLDER
from app.channels.whatsapp_config import (
    WHATSAPP_PLAIN_FIELDS,
    WHATSAPP_SECRET_FIELDS,
    load_whatsapp_config,
)
from app.channels.whatsapp_sender import SendResult, WhatsAppSendError, send_text
from app.core.conversation import HANDOFF_PAUSE_WINDOW
from app.core.exchange_models import ExchangeRequest, ExchangeStatus
from app.core.phone import normalize_phone
from app.deps import build_provider, get_container
from app.jobs.outbox_drain import OUTCOME_SENT, OUTCOME_SUPPRESSED, send_inline_outbound
from app.knowledge.loader import KINDS, SEEDS_DIR, KnowledgeLoader
from app.providers.base import ProviderErrorKind
from app.providers.registry import get_provider, list_providers
from app.providers.verify import verify_key
from app.ratelimit import limiter
from app.shopify.models import Order
from app.store.base import ConversationSummary, MappingView, OutboundDraft, OutboundView

logger = logging.getLogger("app.admin")

_MAX_BODY = 1_048_576  # 1 MiB — same posture as the webhook edges
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class AdminBodyCapMiddleware:
    """Cap ``/admin`` request bodies at the ASGI layer, before FastAPI parses them.

    Two rejections, so the cap cannot be bypassed at the edge (matching the webhook
    edges' raw-body posture):
    - a ``Content-Length`` over the cap → **413** (payload too large);
    - a body-bearing method (POST/PUT/PATCH) with **no** ``Content-Length`` header
      (e.g. chunked transfer-encoding, which would otherwise skip the header check and
      reach a pre-auth route unbounded) → **411** (length required). Browsers and fetch
      always send Content-Length for a JSON body, so this rejects only unusual clients.

    A router dependency cannot enforce this: FastAPI parses (and may 422 on) the JSON
    body before path dependencies run, so an oversized *invalid* body 422s before any
    cap check. Enforcing at the ASGI layer returns first.
    """

    def __init__(self, app: ASGIApp, max_body: int = _MAX_BODY) -> None:
        self._app = app
        self._max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if isinstance(path, str) and path.startswith("/admin"):
                content_length: str | None = None
                for name, value in scope.get("headers") or []:
                    if name == b"content-length":
                        content_length = value.decode("latin-1")
                        break
                if content_length is not None:
                    if content_length.isdigit() and int(content_length) > self._max_body:
                        await self._reject(scope, receive, send, 413, "payload too large")
                        return
                elif _has_body_method(scope):
                    await self._reject(scope, receive, send, 411, "length required")
                    return
        await self._app(scope, receive, send)

    @staticmethod
    async def _reject(
        scope: Scope, receive: Receive, send: Send, status_code: int, message: str
    ) -> None:
        response = PlainTextResponse(message, status_code=status_code)
        await response(scope, receive, send)


def _has_body_method(scope: Scope) -> bool:
    method = scope.get("method", "")
    return isinstance(method, str) and method in _BODY_METHODS


_ADMIN_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (
        b"content-security-policy",
        b"default-src 'self'; style-src 'self' 'unsafe-inline'; "
        b"base-uri 'none'; frame-ancestors 'none'",
    ),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
)
_ADMIN_SECURITY_HEADER_NAMES: frozenset[bytes] = frozenset(
    name for name, _ in _ADMIN_SECURITY_HEADERS
)


class AdminSecurityHeadersMiddleware:
    """Attach CSP + framing/sniffing/referrer/no-store headers to every ``/admin`` response.

    Covers the JSON API and the static panel. The CSP matches the panel: ``default-src 'self'``
    allows the same-origin ``admin.js``; ``style-src 'self' 'unsafe-inline'`` covers the inline
    ``<style>`` block and ``style="..."`` attributes; scripts stay ``'self'`` only (no inline
    ``'unsafe-inline'``). ``Cache-Control: no-store`` matters because the mappings/outbox views
    return customer phone PII that must never be cached by a browser or proxy.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (isinstance(path, str) and path.startswith("/admin")):
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept = [
                    (name, value)
                    for name, value in (message.get("headers") or [])
                    if name.lower() not in _ADMIN_SECURITY_HEADER_NAMES
                ]
                kept.extend(_ADMIN_SECURITY_HEADERS)
                message["headers"] = kept
            await send(message)

        await self._app(scope, receive, send_with_headers)


admin_router = APIRouter(prefix="/admin")


def _audit(
    action: str, outcome: str, *, resource: str | None = None, detail: str | None = None
) -> None:
    """Actor-free admin audit line (item 4).

    One shared ADMIN_PASSWORD means there is no per-user identity to record. Logs the
    action, the resource/field NAME, the outcome, and an optional non-PII detail ONLY —
    never a value (passwords, API keys, credential contents, knowledge bodies, or the erased
    phone number). Lands in Vercel's function logs; no new infra for v1.
    """
    res = "" if resource is None else f" resource={resource}"
    det = "" if detail is None else f" {detail}"
    logger.info("admin_audit action=%s%s outcome=%s%s", action, res, outcome, det)


def _phone_fingerprint(phone: str) -> str:
    """Recomputable, non-reversible HMAC tag for a phone — safe to write to the audit log.

    Keyed with the app's own APP_MASTER_KEY (the Fernet key, reused here as an HMAC key — no
    new secret is stored or derived). A reviewer can recompute it from a customer's number to
    correlate an erasure request, but it is not PII at rest and not reversible without the key.
    """
    key = get_container().settings.app_master_key
    return hmac.new(key.encode(), phone.encode(), hashlib.sha256).hexdigest()[:16]


def _now() -> datetime:
    return datetime.now(UTC)


_MAX_RESOURCE_LEN = 128
# Strip C0 controls (incl. \n \r \t) and DEL — a newline would split the log line and let an
# unauthenticated caller forge a second, fake audit record.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_path(raw: str) -> str:
    """Make a raw request path safe to embed in a single audit log field.

    Removes every control character (newlines/CRs included) BEFORE the value reaches the logger
    — never relying on the logging call to sanitize — and truncates to a fixed bound so one
    request can't emit a multi-kilobyte log-flooding line.
    """
    return _CONTROL_CHARS_RE.sub("", raw)[:_MAX_RESOURCE_LEN]


def _audit_resource(request: Request) -> str:
    """Resource name for an authz-denied audit line.

    Prefer the matched route TEMPLATE (e.g. ``/admin/knowledge/{kind}``) — it is a fixed pattern
    the attacker cannot influence, so it cannot carry an injection payload. Only when no route
    matched (``scope['route']`` absent) do we fall back to a sanitized+bounded raw path.
    """
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str):
        return template
    return _sanitize_path(request.url.path)


def require_admin(request: Request, admin_session: str | None = Cookie(default=None)) -> None:
    settings = get_container().settings
    if not admin_session or not verify_token(settings.app_master_key, admin_session, _now()):
        # Leave an audit trail for forged/absent-cookie attempts (naming the route, not a value).
        _audit("authz", "denied", resource=_audit_resource(request))
        raise HTTPException(status_code=401, detail="admin auth required")


class LoginRequest(BaseModel):
    password: str = Field(max_length=256)


# Owner-directed (client-decisions-all.md Q20/A, confirmed 30-day duration, 2026-08-19): a
# device already logged in should rarely need the password again. Single source of truth for
# BOTH issue_token's ttl_hours and the cookie's max_age, so the two can never independently
# drift out of sync (the prior code kept them in sync only via a comment).
ADMIN_SESSION_TTL_HOURS = 24 * 30


@admin_router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, req: LoginRequest, response: Response) -> dict[str, bool]:
    """Verify the admin password and issue a signed session cookie on success."""
    settings = get_container().settings
    if not settings.admin_password:
        _audit("login", "not_configured")
        raise HTTPException(status_code=503, detail="admin not configured")
    if not check_password(req.password, settings.admin_password):
        _audit("login", "failure")
        raise HTTPException(status_code=401, detail="invalid password")
    _audit("login", "success")
    token = issue_token(settings.app_master_key, _now(), ttl_hours=ADMIN_SESSION_TTL_HOURS)
    response.set_cookie(
        "admin_session",
        token,
        httponly=True,
        samesite="strict",
        # Secure derived from server config, never a client header: prod (TLS-terminated at
        # Vercel) always forces Secure; dev/tests over http stay usable (app_env defaults to dev).
        secure=settings.app_env == "prod",
        max_age=ADMIN_SESSION_TTL_HOURS * 3600,
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
        # Drop url/input/context: a custom field_validator's ValueError leaves a raw,
        # non-JSON-serializable object in ctx["error"] (→ 500 on render); input may carry
        # user data. Same three flags as the global RequestValidationError handler.
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_url=False, include_input=False, include_context=False),
        ) from exc
    repo = get_container().config_repo
    await repo.set_knowledge_override(kind, content)
    await repo.bump_config_int("knowledge_version")
    _audit("knowledge_set", "success", resource=f"knowledge:{kind}")
    return {"ok": True}


class ShopifyCredsRequest(BaseModel):
    """Blank/omitted field = keep the stored value (first-time setup requires client id+secret).

    ``webhook_signing_secret`` is the per-store secret shown on Admin -> Settings ->
    Notifications, used to sign webhooks the owner creates there. It is INDEPENDENT of the
    app's client_id/client_secret and additive — omitting it never affects first-time setup.
    """

    client_id: str | None = Field(default=None, max_length=256)
    client_secret: str | None = Field(default=None, max_length=256)
    webhook_signing_secret: str | None = Field(default=None, max_length=256)


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
    has_webhook_secret = await cfg.get_secret("shopify:webhook_signing_secret") is not None
    return {
        "configured": has_id and has_secret,
        "webhook_signing_secret_configured": has_webhook_secret,
    }


@admin_router.post("/shopify", dependencies=[Depends(require_admin)])
async def set_shopify(req: ShopifyCredsRequest) -> dict[str, bool]:
    cfg = get_container().config
    client_id, client_secret = _clean(req.client_id), _clean(req.client_secret)
    webhook_signing_secret = _clean(req.webhook_signing_secret)
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
    # Additive transition secret: written only when supplied, independent of the client creds.
    if webhook_signing_secret is not None:
        await cfg.set_secret("shopify:webhook_signing_secret", webhook_signing_secret)
    _audit("credential_set", "success", resource="shopify")
    return {"ok": True}


_WA_ALL_FIELDS: tuple[str, ...] = WHATSAPP_SECRET_FIELDS + WHATSAPP_PLAIN_FIELDS


class WhatsAppCredsRequest(BaseModel):
    """All optional: blank/omitted keeps the stored value; first-time needs all six.

    ``phone_number_id``/``waba_id`` are digits-only and ``api_version`` is ``vNN.N`` so
    path-like junk cannot be stored and interpolated into the Graph API URL.
    """

    phone_number_id: str | None = Field(default=None, max_length=64, pattern=r"^\d{5,20}$")
    waba_id: str | None = Field(default=None, max_length=64, pattern=r"^\d{5,20}$")
    api_version: str | None = Field(default=None, max_length=16, pattern=r"^v\d+\.\d+$")
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
    _audit("credential_set", "success", resource="whatsapp")
    return {"ok": True}


_KIND_MESSAGES: dict[ProviderErrorKind, str] = {
    ProviderErrorKind.AUTH: "The API key was rejected by the provider. Please check the key.",
    ProviderErrorKind.RATE_LIMIT: (
        "The key looks valid, but the provider reports the quota or billing limit is"
        " exhausted for this model. Check your plan or billing."
    ),
    ProviderErrorKind.NOT_FOUND: (
        "The key looks valid, but the configured model is not available to this key."
    ),
    ProviderErrorKind.TIMEOUT: "Could not reach the provider to verify the key. Please retry.",
    # Generic, never the raw litellm/vendor exception text (matches jobs/router.py posture).
    ProviderErrorKind.UNKNOWN: "Could not verify the key with the provider.",
}

_RATE_LIMIT_SAVE_WARNING = (
    "Key saved. The model is rate-limited right now; replies may be briefly delayed."
)

# Safe fallback for env-provider (Vertex) verification failures. The raw provider error may
# embed the service-account JSON, so it is NEVER surfaced for env-auth providers.
_ENV_VERIFY_FAILED = (
    "Could not verify the provider credentials. Check the service-account JSON, "
    "project, and location."
)

# Env-auth (Vertex) failures have NO api_key — messages reference the service-account JSON,
# project, and location instead of "the key". Kept separate from _KIND_MESSAGES (api_key wording).
_ENV_KIND_MESSAGES: dict[ProviderErrorKind, str] = {
    ProviderErrorKind.AUTH: (
        "The service-account credentials were rejected by the provider. Check the"
        " service-account JSON, project, and location."
    ),
    ProviderErrorKind.RATE_LIMIT: (
        "The credentials look valid, but the provider reports the quota or billing limit is"
        " exhausted for this model, project, and location. Check your plan or billing."
    ),
    ProviderErrorKind.NOT_FOUND: (
        "The credentials look valid, but the configured model is not available. Check the"
        " model, project, and location."
    ),
    ProviderErrorKind.TIMEOUT: (
        "Could not reach the provider to verify the service-account JSON, project, and"
        " location. Please retry."
    ),
}


def _env_verify_detail(kind: ProviderErrorKind | None) -> str:
    """Safe 400 detail for an env-auth verification failure — never the raw provider error.

    Known kinds get a service-account-oriented message; UNKNOWN/None fall back to the generic
    Vertex message so the service-account JSON in a raw error can never leak into the response.
    """
    if kind is not None:
        mapped = _ENV_KIND_MESSAGES.get(kind)
        if mapped is not None:
            return mapped
    return _ENV_VERIFY_FAILED


class ProviderConfigRequest(BaseModel):
    provider: str = Field(max_length=64)
    api_key: str | None = Field(default=None, max_length=512)


@admin_router.get("/providers", dependencies=[Depends(require_admin)])
async def providers() -> list[dict[str, str]]:
    return [
        {
            "key": p.key,
            "label": p.label,
            "default_model": p.default_model,
            "auth_kind": p.auth_kind,
        }
        for p in list_providers()
    ]


@admin_router.get("/config", dependencies=[Depends(require_admin)])
async def provider_config() -> dict[str, object]:
    """Active provider status — NEVER returns the api_key."""
    cfg = get_container().config
    active = await cfg.get_plain("llm:active_provider")
    if not active:
        return {"configured": False, "provider": None}
    info = get_provider(active)
    if info is not None and info.auth_kind == "env":
        # Env-credential provider (Vertex): credentials come from env, no stored key needed.
        return {"configured": True, "provider": active}
    has_key = await cfg.get_secret(f"llm:api_key:{active}") is not None
    return {"configured": has_key, "provider": active if has_key else None}


@admin_router.post("/provider", dependencies=[Depends(require_admin)])
async def set_provider(req: ProviderConfigRequest) -> dict[str, bool | str]:
    """Verify the provider credentials BEFORE persisting.

    api_key providers: verify the pasted key, then encrypt-store it and activate.
    env-auth providers (Vertex): verify against env creds (no key), then activate WITHOUT
    storing any key. On env-auth failure only a safe message is returned — the raw error may
    embed the service-account JSON.
    """
    info = get_provider(req.provider)
    if info is None:
        raise HTTPException(status_code=400, detail=f"unknown provider: {req.provider}")
    verifier = build_provider(get_container().settings)
    cfg = get_container().config

    if info.auth_kind == "env":
        result = await verify_key(
            verifier, info.default_model, "", extra_params=info.request_params
        )
        if not result.ok:
            raise HTTPException(status_code=400, detail=_env_verify_detail(result.kind))
        # Activate without storing a key — env-auth credentials live in env, never the DB.
        await cfg.set_plain("llm:active_provider", req.provider)
        _audit("provider_set", "success", resource=f"llm:{req.provider}")
        return {"ok": True}

    if not req.api_key:
        raise HTTPException(status_code=400, detail="api_key is required")
    result = await verify_key(
        verifier, info.default_model, req.api_key, extra_params=info.request_params
    )
    if not result.ok:
        if result.kind is ProviderErrorKind.RATE_LIMIT and info.accept_on_rate_limit:
            await cfg.set_secret(f"llm:api_key:{req.provider}", req.api_key)
            await cfg.set_plain("llm:active_provider", req.provider)
            _audit("provider_set", "success", resource=f"llm:{req.provider}")
            return {"ok": True, "warning": _RATE_LIMIT_SAVE_WARNING}
        detail = (
            _KIND_MESSAGES.get(result.kind, result.error or "verification failed")
            if result.kind
            else (result.error or "verification failed")
        )
        raise HTTPException(status_code=400, detail=detail)
    await cfg.set_secret(f"llm:api_key:{req.provider}", req.api_key)
    await cfg.set_plain("llm:active_provider", req.provider)
    _audit("provider_set", "success", resource=f"llm:{req.provider}")
    return {"ok": True}


@admin_router.get("/controls", dependencies=[Depends(require_admin)])
async def get_controls() -> AdminControls:
    return await load_controls(get_container().config)


@admin_router.put("/controls", dependencies=[Depends(require_admin)])
async def put_controls(controls: AdminControls) -> dict[str, bool]:
    await save_controls(get_container().config, controls)
    _audit("controls_set", "success", resource="controls")
    return {"ok": True}


class ManualReplyRequest(BaseModel):
    """Free-text message the admin types to send directly to a customer's WhatsApp."""

    text: str = Field(max_length=4096)


class ExchangeUpdateRequest(BaseModel):
    """Admin-driven advance of an exchange request's status and/or return-tracking link.

    Both fields optional -- a single call can set either, both, or (rejected below) neither.
    """

    status: ExchangeStatus | None = None
    return_tracking_url: str | None = Field(default=None, max_length=2048)


class TemplateSendRequest(BaseModel):
    """Admin-editable resend of one of the store's approved WhatsApp templates for an order.

    Length-bounded like the sibling ManualReplyRequest: order_name/template are short catalog/
    order identifiers, and each editable field value is capped near Meta's own ~1024-char
    template-parameter limit so no unbounded string rides in up to the 1 MiB body cap.
    """

    order_name: str = Field(max_length=64)
    template: str = Field(max_length=64)
    values: dict[str, Annotated[str, StringConstraints(max_length=1024)]] = Field(
        default_factory=dict
    )


class ErasureRequest(BaseModel):
    """DPDP right-to-erasure by phone number (E.164). The phone is PII — never logged.

    ASCII digits only: pydantic-core's ``\\d`` is Unicode-aware, so Arabic-Indic/fullwidth
    digit strings would pass yet match zero real rows (a false "success"). ``[0-9]`` pins it.
    """

    phone: str = Field(max_length=32, pattern=r"^\+[0-9]{7,15}$")


@admin_router.post("/erasure", dependencies=[Depends(require_admin)])
@limiter.limit("10/minute")
async def erase_by_phone(request: Request, req: ErasureRequest) -> dict[str, object]:
    """Purge every row keyed to one phone number across the retention tables.

    Works today regardless of the (pending) retention period. Destructive and irreversible, so
    it is rate-limited like /login. Audit-logged as an erasure for *a* number — the number is
    never written to the log line; the resource is an HMAC fingerprint (recomputable, not PII).
    """
    result = await get_container().ingest.delete_by_phone(req.phone)
    deleted_total = sum(asdict(result).values())
    _audit(
        "erasure",
        "success",
        resource=f"phone:{_phone_fingerprint(req.phone)}",
        detail=f"deleted={deleted_total}",
    )
    return {"ok": True, "deleted": asdict(result)}


@admin_router.get("/mappings", dependencies=[Depends(require_admin)])
async def list_mappings(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, object]]:
    rows: list[MappingView] = await get_container().ingest.recent_mappings(limit)
    return [asdict(r) for r in rows]


@admin_router.get("/outbox", dependencies=[Depends(require_admin)])
async def list_outbox(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, object]]:
    rows: list[OutboundView] = await get_container().ingest.recent_outbound(limit)
    return [asdict(r) for r in rows]


# body_params values trace back to Shopify webhook data (signed but attacker-typed under this
# repo's threat model). Cap each value and the joined total so one oversized param cannot inflate
# the response/DOM, matching list_conversations' existing 120-char preview truncation posture.
_TEMPLATE_VALUE_MAX = 200
_TEMPLATE_TEXT_MAX = 500


def _template_sent_text(payload_json: str) -> str:
    try:
        data = json.loads(payload_json)
    except (ValueError, TypeError):
        return "(unreadable template payload)"
    # json.loads("null"/"42"/"[]") parses successfully to a non-dict -- guard before .get so a
    # valid-but-non-dict payload degrades to the fallback instead of raising AttributeError (500).
    if not isinstance(data, dict):
        return "(unreadable template payload)"
    template = str(data.get("template", "?"))[:_TEMPLATE_VALUE_MAX]
    body_params = data.get("body_params")
    if isinstance(body_params, dict):
        raw_values: list[object] = list(body_params.values())
    elif isinstance(body_params, list):
        raw_values = body_params
    else:
        raw_values = []
    values = ", ".join(str(v)[:_TEMPLATE_VALUE_MAX] for v in raw_values)[:_TEMPLATE_TEXT_MAX]
    return f"{template} → {values}" if values else template


# Best-effort reconstruction of each approved template's approximate customer-facing wording, for
# admin-display purposes only (never sent anywhere) -- Meta's literal approved copy isn't stored
# in this codebase. Positional templates use a numbered-args format string; named templates use
# the exact payload_json body_params keys each one is built with (see
# docs/superpowers/specs/2026-08-15-order-type-routing-templated-replies-design.md and
# 2026-08-16-fulfillment-lifecycle-notifications-design.md for each template's param shape).
_TEMPLATE_MESSAGE_TEMPLATES: dict[str, str] = {
    "cod_confirmmsg": "Hi {0}, your order {1} has been confirmed. We will ship it soon.",
    "cod_cancel": "Hi {0}, your order {1} has been cancelled as requested.",
    "order_shipped": "Hi {0}, your order {1} has shipped via {2}. Track it here: {3}",
    "order_delivered": "Hi {0}, your order {1} has been delivered. Thank you for shopping with us!",
    "cod_confirmation": (
        "Hi {customer_name}, please confirm your Cash on Delivery order {order_id} for "
        "{product_name} ({product_color}, {product_size}) — {product_amount}."
    ),
    "prepaid_order": (
        "Hi {customer_name}, your order {order_id} for {product_name} ({product_color}, "
        "{product_size}) — {product_amount} has been received."
    ),
}


def _template_message_text(payload_json: str) -> str:
    """Reconstruct an approximate customer-facing message for a template_sent bubble.

    Falls back to _template_sent_text()'s raw "template -> params" format whenever the template
    name isn't mapped, the payload isn't a dict, or substitution fails (wrong param count/shape)
    -- this must never raise, a malformed row should degrade, not 500 the whole thread view.
    """
    try:
        data = json.loads(payload_json)
    except (ValueError, TypeError):
        return _template_sent_text(payload_json)
    if not isinstance(data, dict):
        return _template_sent_text(payload_json)
    template = data.get("template")
    fmt = _TEMPLATE_MESSAGE_TEMPLATES.get(str(template)) if isinstance(template, str) else None
    if fmt is None:
        return _template_sent_text(payload_json)
    body_params = data.get("body_params")
    try:
        if isinstance(body_params, dict):
            return fmt.format(**body_params)
        if isinstance(body_params, list):
            return fmt.format(*body_params)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        # Widened past KeyError/IndexError so a future template with a format-spec ({0:.2f}) or
        # nested lookup ({0.name}) can't raise ValueError/AttributeError/TypeError and break this
        # function's must-never-raise guarantee -- a malformed row degrades, it never 500s.
        return _template_sent_text(payload_json)
    return _template_sent_text(payload_json)


def _button_tap_text(action: str, result: str) -> str:
    return f"Tapped {action} → {result}"


@admin_router.get("/conversations", dependencies=[Depends(require_admin)])
async def list_conversations(
    # Max lowered 500 -> 100 (each displayed thread costs one find_messages preview query, so a
    # high limit is a per-request N+1 amplifier on the shared pool; admin-only, so a cap suffices).
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict[str, object]]:
    c = get_container()

    # Union distinct phones across all THREE sources so a customer who only ever received an order
    # confirmation (no conversation row) still appears. Normalize every candidate to E.164 before
    # deduping so one customer is never listed twice under two formats. get_or_create below is a
    # display-only lookup: its ON CONFLICT branch is a self-assignment (no bump on an existing
    # row), and recent_conversations() excludes any message-less row entirely -- so a brand-new
    # row's creation-time DEFAULT now() stamp can never leak into this union as fake recency.
    last_active_by_phone: dict[str, str | None] = {}
    ordered_phones: list[str] = []
    seen: set[str] = set()

    def _add(raw: str, last_active: str | None) -> None:
        norm = normalize_phone(raw)
        if norm is None:
            return
        if norm not in seen:
            seen.add(norm)
            ordered_phones.append(norm)
        # Take the MAXIMUM stamp across all three sources for a phone, not first-non-None-wins:
        # a stale conversations.last_active_at must never mask a genuinely more-recent
        # outbound_messages/order_actions timestamp for the same phone. ISO-8601 strings compare
        # lexicographically in timestamp order (same convention the sorts below rely on).
        if last_active is not None:
            existing = last_active_by_phone.get(norm)
            if existing is None or last_active > existing:
                last_active_by_phone[norm] = last_active

    summaries: list[ConversationSummary] = await c.conversations.recent_conversations(limit)
    for s in summaries:
        _add(s.user_id, s.last_active_at)
    # Each source returns (identifier, latest_iso) ordered by recency, so an outbound-only or
    # tap-only customer carries its REAL last-active stamp into the union sort below (not None) --
    # and each source's own LIMIT already keeps its most-recent, not a lexicographic-first, slice.
    for phone, last_active in await c.ingest.distinct_outbound_phones(limit):
        _add(phone, last_active)
    for wa, last_active in await c.ingest.distinct_order_action_wa_ids(limit):
        _add(wa, last_active)

    # Truncate to `limit` BEFORE the per-thread queries so the union (up to 3*limit candidates)
    # cannot amplify the preview/get_or_create work beyond `limit` rows. Order by the captured
    # (MAX-across-sources) last_active so the most-recently-active threads are the ones we
    # materialize; every source supplies a real recency stamp, so no source sorts artificially last.
    ordered_phones.sort(key=lambda p: str(last_active_by_phone.get(p) or ""), reverse=True)
    ordered_phones = ordered_phones[:limit]

    result: list[dict[str, object]] = []
    for norm in ordered_phones:
        # Materialize a stable conversation.id for the thread (idempotent -- repeated list views
        # fetch the existing id, they do not create duplicates). This is the one place a create-on-
        # miss is intended: the next sub-project (manual send) needs every thread to have an id.
        thread_id = await c.conversations.get_or_create(norm)
        recent = await c.conversations.find_messages_by_user_id(norm, limit=1)
        preview = recent[-1].content[:120] if recent else ""
        # Prefer the latest message timestamp; fall back to the MAX-across-sources last_active
        # captured above (used for outbound-only / tap-only threads that have no messages).
        last_active = recent[-1].created_at if recent else last_active_by_phone.get(norm)

        orders_for_phone = await c.ingest.find_mirrored_orders_by_phone(norm)
        orders_sorted_for_name = sorted(
            orders_for_phone, key=lambda o: str(o.updated_at or ""), reverse=True
        )
        customer_name: str | None = None
        if orders_sorted_for_name and orders_sorted_for_name[0].customer:
            cust = orders_sorted_for_name[0].customer
            name = " ".join(p for p in (cust.first_name, cust.last_name) if p).strip()
            customer_name = name or None
        order_names = [o.name for o in orders_for_phone]

        unread_count = await c.conversations.count_unread_messages(thread_id)
        paused_until = await c.conversations.get_paused_until(thread_id)
        ai_paused = paused_until is not None and paused_until > datetime.now(UTC)

        result.append(
            {"thread_id": thread_id, "phone": norm, "last_active_at": last_active,
             "preview": preview, "customer_name": customer_name, "order_names": order_names,
             "unread_count": unread_count, "ai_paused": ai_paused}
        )

    result.sort(key=lambda r: str(r["last_active_at"] or ""), reverse=True)
    return result


def _order_summary(
    order: Order, exchange: ExchangeRequest | None = None,
) -> dict[str, object]:
    tracking_company = tracking_number = tracking_url = None
    if order.fulfillments:
        first = order.fulfillments[0]
        tracking_company = first.tracking_company
        tracking_number = first.tracking_number
        tracking_url = first.tracking_url
    customer_name = None
    address_line1 = address_line2 = city = state = postal_code = None
    if order.customer:
        name = " ".join(
            p for p in (order.customer.first_name, order.customer.last_name) if p
        ).strip()
        customer_name = name or None
        address_line1 = order.customer.address_line1
        address_line2 = order.customer.address_line2
        city = order.customer.city
        state = order.customer.state
        postal_code = order.customer.postal_code
    summary: dict[str, object] = {
        "order_name": order.name,
        "financial_status": order.financial_status,
        "fulfillment_status": order.fulfillment_status,
        "cancelled_at": order.cancelled_at,
        "is_cod": order.is_cod(),
        "total_amount": order.total.amount if order.total else None,
        "total_currency": order.total.currency if order.total else None,
        "tags": list(order.tags),
        "tracking_company": tracking_company,
        "tracking_number": tracking_number,
        "tracking_url": tracking_url,
        "customer_name": customer_name,
        "address_line1": address_line1,
        "address_line2": address_line2,
        "city": city,
        "state": state,
        "postal_code": postal_code,
        "line_items": [
            {
                "title": li.title,
                "quantity": li.quantity,
                "variant_title": li.variant_title,
                "price_amount": li.price.amount if li.price else None,
                "price_currency": li.price.currency if li.price else None,
            }
            for li in order.line_items
        ],
    }
    if exchange is not None:
        summary["exchange"] = {
            "id": exchange.id,
            "requested_size": exchange.requested_size,
            "status": exchange.status,
            "return_tracking_url": exchange.return_tracking_url,
        }
    return summary


@admin_router.get("/conversations/{thread_id}", dependencies=[Depends(require_admin)])
async def get_conversation_thread(thread_id: int) -> dict[str, object]:
    c = get_container()
    # Resolve the conversation's normalized phone from the opaque id; a bad literal id genuinely
    # does not exist -> 404 (distinct from "no messages yet", which is a real empty thread).
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="thread not found")

    entries: list[dict[str, object]] = []

    for msg in await c.conversations.find_messages_by_user_id(user_id, limit=200):
        entry: dict[str, object] = {
            "type": "customer_message" if msg.role == "user" else "ai_reply",
            "timestamp": msg.created_at,
            "text": msg.content,
        }
        # delivery_status only applies to an outbound (assistant) send; a customer_message has no
        # send-direction, so the field is added only for ai_reply entries.
        if msg.role == "assistant":
            entry["delivery_status"] = msg.delivery_status
            entry["sender"] = msg.sender
        entries.append(entry)

    # user_id is the normalized E.164 phone; outbound rows are keyed on that same form.
    for row in await c.ingest.find_outbound_by_phone(user_id, limit=200):
        entries.append({
            "type": "template_sent",
            "timestamp": row.created_at,
            "text": _template_message_text(row.payload_json),
            "status": row.state,
            "delivery_status": row.delivery_status,
        })

    # order_actions.actor_wa_id is written RAW (no +); query with BOTH the normalized and the
    # digits-only candidate so button taps merge into the same thread (read-side fix for finding
    # #1 -- the write format in core/order_actions.py is deliberately left untouched).
    candidate_wa_ids = list({user_id, user_id.lstrip("+")})
    for action in await c.ingest.find_order_actions_by_wa_ids(candidate_wa_ids, limit=200):
        entries.append({
            "type": "button_tap",
            "timestamp": action.created_at,
            "text": _button_tap_text(action.action, action.result),
        })

    # Timestamps are ISO 8601 strings (or None -> ""), which sort lexicographically in
    # chronological order. str() keeps the key type mypy-checkable (object -> str).
    entries.sort(key=lambda e: str(e["timestamp"] or ""))

    orders = await c.ingest.find_mirrored_orders_by_phone(user_id)
    orders_sorted = sorted(orders, key=lambda o: str(o.updated_at or ""), reverse=True)
    exchanges_by_order_gid = {
        e.order_gid: e for e in await c.exchanges.list_for_phone(user_id)
    }
    order_summaries = [
        _order_summary(o, exchanges_by_order_gid.get(o.gid)) for o in orders_sorted
    ]

    paused_until = await c.conversations.get_paused_until(thread_id)
    # Opening a thread (this endpoint fires both on click-open and on every 3s poll tick while it
    # stays open) marks it read. Placed last and guarded so a failure here can never prevent the
    # entries/orders payload from returning -- worst case a badge stays stale one tick, never a
    # broken thread view.
    try:
        await c.conversations.mark_read(thread_id, datetime.now(UTC))
    except Exception:
        logger.warning("failed to mark thread %s as read", thread_id)
    return {
        "entries": entries,
        "orders": order_summaries,
        "paused_until": paused_until.isoformat() if paused_until else None,
    }


@admin_router.post("/exchanges/{exchange_id}", dependencies=[Depends(require_admin)])
async def update_exchange(exchange_id: int, body: ExchangeUpdateRequest) -> dict[str, object]:
    """Advance an exchange request's status and/or set its return-tracking URL.

    No courier/QC integration exists (design doc) -- this is the only way either field ever
    changes after the exchange agent creates the request.
    """
    c = get_container()
    existing = await c.exchanges.get(exchange_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="exchange request not found")
    if body.status is not None:
        await c.exchanges.set_status(exchange_id, body.status)
    if body.return_tracking_url is not None:
        await c.exchanges.set_return_tracking_url(exchange_id, body.return_tracking_url)
    return {"ok": True}


@admin_router.post(
    "/conversations/{thread_id}/resume", dependencies=[Depends(require_admin)]
)
async def resume_conversation(thread_id: int) -> dict[str, object]:
    """Manually clear a conversation's AI handoff pause, putting the AI back in charge.

    Reuses the existing pause_until/get_paused_until primitives (core/conversation.py's pause
    check is ``now < paused_until``) -- setting ``until`` to now is equivalent to clearing the
    pause, so no new store method is needed. A conversation that isn't currently paused is a
    harmless no-op.
    """
    c = get_container()
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="thread not found")
    await c.conversations.pause_until(thread_id, datetime.now(UTC))
    _audit("resume_ai", "success", resource=f"thread:{thread_id}")
    return {"ok": True}


@admin_router.post(
    "/conversations/{thread_id}/messages", dependencies=[Depends(require_admin)]
)
@limiter.limit("30/minute")
async def send_manual_reply(
    request: Request, thread_id: int, body: ManualReplyRequest, response: Response
) -> dict[str, object]:
    """Send a free-text WhatsApp message typed by the admin, mirroring core/conversation.py's
    AI-reply send path so the same delivery-status tick marks and auto-retry machinery apply
    for free (both key off a wamid on a `messages` row, regardless of how the row was created).

    Deliberately bypasses send_decision/send_mode/allowlist_phones -- a manual, targeted admin
    reply is not what the kill switch exists to stop (design decision, see
    docs/superpowers/specs/2026-08-18-admin-manual-reply-design.md). Persisted with
    role="assistant" (so it stays in the AI's memory window and stays eligible for
    delivery_retry.py's role='assistant' filter) and sender="admin" (display-only marker, never
    read by memory/retry logic).

    Send-failure paths return {"ok": false, "error": ...} with a non-2xx status set directly on
    the response object (rather than raising HTTPException) -- the admin UI reads a uniform
    {"ok": bool, ...} body for this endpoint regardless of outcome, unlike the rest of this
    router's error responses.
    """
    c = get_container()
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        _audit("admin_manual_reply", "failure", resource=f"thread:{thread_id}")
        raise HTTPException(status_code=404, detail="thread not found")
    text = body.text.strip()
    if not text:
        _audit("admin_manual_reply", "failure", resource=f"thread:{thread_id}")
        raise HTTPException(status_code=400, detail="text must not be empty")

    message_id = await c.conversations.append_message(
        thread_id, "assistant", text, sender="admin"
    )

    wa_cfg = await load_whatsapp_config(c.config)
    if wa_cfg is None:
        # The row was persisted before this guard; mark it failed so the UI shows the red "!"
        # tick (not a misleading grey "sent" tick) and delivery_retry never treats it as sent.
        await c.conversations.set_message_delivery_status(message_id, "failed")
        _audit("admin_manual_reply", "failure", resource=f"thread:{thread_id}")
        raise HTTPException(status_code=503, detail="whatsapp not configured")

    try:
        result: SendResult = await send_text(c.http, wa_cfg, user_id, text)
    except WhatsAppSendError:
        # Do NOT pause the AI on a failed send: unlike the customer-initiated handoff path, a
        # misconfigured/failing send would otherwise silently mute the AI for 24h with no reply
        # and no owner alert. Mark the row failed (no wamid ever arrived) so it shows the red "!".
        await c.conversations.set_message_delivery_status(message_id, "failed")
        _audit("admin_manual_reply", "failure", resource=f"thread:{thread_id}")
        response.status_code = 502
        return {"ok": False, "error": "failed to send message"}

    if not result.ok:
        await c.conversations.set_message_delivery_status(message_id, "failed")
        _audit("admin_manual_reply", "failure", resource=f"thread:{thread_id}")
        response.status_code = 502
        return {"ok": False, "error": result.error or "send failed"}

    # Pause the AI only on a genuinely successful send (mirrors the handoff pause window).
    await c.conversations.pause_until(thread_id, datetime.now(UTC) + HANDOFF_PAUSE_WINDOW)

    if result.wamid:
        try:
            await c.conversations.set_message_wamid(message_id, result.wamid)
        except Exception:
            logger.exception("failed to record wamid for manual admin reply")

    _audit("admin_manual_reply", "success", resource=f"thread:{thread_id}")
    return {"ok": True, "wamid": result.wamid}


_CONFIRMATION_TEMPLATES = ("cod_confirmation", "prepaid_order")
_FULFILLMENT_TEMPLATES = ("order_shipped", "order_delivered")


def _template_applies_to_order(template_key: str, order: Order) -> bool:
    """Filter the offered templates by the order's current state (Finding 10 -- support noise, not
    a mutation-safety gate; core/order_actions.py re-validates every button tap independently).

    Skip the Confirm/Cancel confirmation templates for an already-cancelled order, and skip the
    shipped/delivered notices for an order with no fulfillments yet (nothing to report on). Uses
    only the Order fields already in hand -- no extra Shopify call.
    """
    if template_key in _CONFIRMATION_TEMPLATES and order.is_cancelled():
        return False
    if template_key in _FULFILLMENT_TEMPLATES and not order.fulfillments:
        return False
    return True


@admin_router.get(
    "/conversations/{thread_id}/templates", dependencies=[Depends(require_admin)]
)
async def list_templates(thread_id: int) -> dict[str, object]:
    """List every order for this thread's customer with each catalog template's fields
    pre-filled from that order's current data -- purely a read, no send happens here."""
    c = get_container()
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="thread not found")
    orders = await c.ingest.find_mirrored_orders_by_phone(user_id)
    order_payloads: list[dict[str, object]] = []
    for order in orders:
        defaults = resolve_template_defaults(order)
        templates = [
            {
                "key": key,
                "label": tmpl.label,
                "has_buttons": tmpl.has_confirm_cancel_buttons,
                "fields": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "value": defaults.get(f.default_from, ""),
                        # order_id is the server-pinned order identity (see send_admin_template):
                        # the send path force-overwrites it, so it is shown read-only, not editable.
                        "read_only": f.key == "order_id",
                    }
                    for f in tmpl.fields
                ],
            }
            for key, tmpl in TEMPLATE_CATALOG.items()
            if _template_applies_to_order(key, order)
        ]
        order_payloads.append({"order_name": order.name, "templates": templates})
    return {"orders": order_payloads}


@admin_router.post(
    "/conversations/{thread_id}/templates", dependencies=[Depends(require_admin)]
)
@limiter.limit("30/minute")
async def send_admin_template(
    request: Request, thread_id: int, body: TemplateSendRequest, response: Response
) -> dict[str, object]:
    """Resend one of the store's approved templates for a specific order, with admin-editable
    field values, via the SAME enqueue_outbound + send_inline_outbound pipeline every automatic
    template send already uses (order confirmation, shipped, delivered) -- so it respects
    send_decision/send_mode/allowlist_phones exactly like those, UNLIKE send_manual_reply's free
    text (which deliberately bypasses the kill switch; a template resend does not, since it's the
    same category of message as every other automatic template send).

    The order's gid (used for cod_confirmation's Confirm/Cancel buttons) is ALWAYS resolved here,
    server-side, from find_mirrored_orders_by_phone -- `body` never carries a gid at all, only the
    human-readable order_name, so there is no way for a request to target a button payload at an
    order it didn't already have visibility into via this same thread.
    """
    c = get_container()
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        _audit("admin_template_resend", "failure", resource=f"thread:{thread_id}")
        raise HTTPException(status_code=404, detail="thread not found")

    tmpl = TEMPLATE_CATALOG.get(body.template)
    if tmpl is None:
        _audit("admin_template_resend", "failure", resource=f"thread:{thread_id}")
        raise HTTPException(status_code=400, detail="unknown template")

    orders = await c.ingest.find_mirrored_orders_by_phone(user_id)
    order = next((o for o in orders if o.name == body.order_name), None)
    if order is None:
        _audit("admin_template_resend", "failure", resource=f"thread:{thread_id}")
        raise HTTPException(status_code=404, detail="order not found for this customer")

    defaults = resolve_template_defaults(order)
    resolved: dict[str, str] = {
        f.key: (body.values.get(f.key) or defaults.get(f.default_from, "") or "")
        for f in tmpl.fields
    }
    # Finding 3: pin the order-identity field to the SERVER-resolved order name, overriding any
    # admin submission, so the visible order text can never desync from the Confirm/Cancel button
    # payloads (which are always the server gid). order_id is the field key only on the two
    # confirmation templates; practically only cod_confirmation has buttons, but prepaid_order's
    # text would be just as misleading with a mismatched id, so both are pinned.
    if "order_id" in resolved:
        resolved["order_id"] = order.name
    # Finding 1: Meta rejects an empty template body parameter (named or positional), so every
    # send call site substitutes EMPTY_PARAM_PLACEHOLDER rather than sending "" (see
    # app.channels.copy). A blank admin override + blank default (e.g. resending order_shipped for a
    # not-yet-fulfilled order -> blank courier/tracking) would otherwise make Meta reject the send.
    resolved = {k: (v if v.strip() else EMPTY_PARAM_PLACEHOLDER) for k, v in resolved.items()}
    body_params: dict[str, str] | list[str]
    if tmpl.param_style == "named":
        body_params = resolved
    else:
        body_params = [resolved[f.key] for f in tmpl.fields]
    button_payloads = (
        [f"order:confirm:{order.gid}", f"order:cancel:{order.gid}"]
        if tmpl.has_confirm_cancel_buttons else []
    )

    # Prefix is "admin_resend:" (registered in outbox_drain.py's _DEDUPE_PREFIXES), followed
    # immediately by the order's gid (required: _gid_from_dedupe_key needs the text right after
    # the matched prefix to start with "gid://"), then the template key and a fresh uuid so a
    # repeat resend of the SAME template for the SAME order never collides on dedupe_key -- this
    # is a deliberate repeat, not a dedupe-guarded automatic trigger.
    # WARNING (Finding 9): _gid_from_dedupe_key's return value for this "admin_resend:" prefix is
    # NOT a clean order gid -- it is "gid://shopify/Order/123:cod_confirmation:<uuid>" (trailing
    # template-key + uuid text). It passes the startswith("gid://") check but must NEVER be used
    # for anything beyond a truthy/None validity check. The ONLY mapping-status write in the drain
    # is gated on the DIFFERENT order-confirmation prefix family, so this is safe today; do not add
    # any new gid-consuming behavior keyed off an admin_resend dedupe_key.
    draft = OutboundDraft(
        dedupe_key=f"admin_resend:{order.gid}:{body.template}:{uuid.uuid4()}",
        kind="admin_template_resend",
        phone_e164=user_id,
        payload_json=json.dumps(
            {"template": body.template, "language": tmpl.language, "body_params": body_params,
             "buttons": button_payloads}
        ),
    )
    outbound_id = await c.ingest.enqueue_outbound(draft)
    if outbound_id is None:
        _audit("admin_template_resend", "failure", resource=f"thread:{thread_id}")
        response.status_code = 502
        return {"ok": False, "error": "failed to queue template"}

    outcome = await send_inline_outbound(c, outbound_id)
    # None means nothing was attempted (send_mode="off", no WhatsApp config, or the row's claim
    # was lost to a race) -- the row stays queued for the backstop drain, which is a SUCCESSFUL
    # enqueue, not a failure the admin needs to retry. OUTCOME_SUPPRESSED means send_decision
    # correctly withheld the send (shadow mode, or allowlist mode with a non-allowlisted phone) --
    # also not a failure, same category as "off" leaving the row queued/withheld by policy.
    # OUTCOME_RETRY (a transport error or non-terminal Meta error on this single inline attempt)
    # is treated as a failure here too -- the admin should retry manually rather than wait for the
    # cron drain's own retry cadence.
    if outcome not in (None, OUTCOME_SENT, OUTCOME_SUPPRESSED):
        _audit("admin_template_resend", "failure", resource=f"thread:{thread_id}")
        response.status_code = 502
        return {"ok": False, "error": f"send failed: {outcome}"}

    # Finding 4: distinguish the three non-failure outcomes for the admin, instead of a bare
    # {"ok": true} that conflates "delivered", "queued for the (not reliably-running) backstop
    # drain", and "permanently withheld by send policy". None -> queued; SENT -> sent; SUPPRESSED
    # -> suppressed. The same distinction is written to the audit detail.
    if outcome == OUTCOME_SENT:
        status = "sent"
    elif outcome == OUTCOME_SUPPRESSED:
        status = "suppressed"
    else:  # None -- nothing attempted, row left queued for the backstop drain
        status = "queued"
    _audit(
        "admin_template_resend", "success",
        resource=f"thread:{thread_id}", detail=f"outcome={status}",
    )
    return {"ok": True, "status": status}
