"""Admin JSON API: login/session now; creds, knowledge, controls, views in later tasks."""

import hashlib
import hmac
import json
import logging
import re
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.admin.auth import check_password, issue_token, verify_token
from app.admin.controls import AdminControls, load_controls, save_controls
from app.admin.knowledge_models import validate_and_serialize
from app.channels.whatsapp_config import (
    WHATSAPP_PLAIN_FIELDS,
    WHATSAPP_SECRET_FIELDS,
    load_whatsapp_config,
)
from app.core.phone import normalize_phone
from app.deps import build_provider, get_container
from app.knowledge.loader import KINDS, SEEDS_DIR, KnowledgeLoader
from app.providers.base import ProviderErrorKind
from app.providers.registry import get_provider, list_providers
from app.providers.verify import verify_key
from app.ratelimit import limiter
from app.store.base import ConversationSummary, MappingView, OutboundView

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
    token = issue_token(settings.app_master_key, _now())
    response.set_cookie(
        "admin_session",
        token,
        httponly=True,
        samesite="strict",
        # Secure derived from server config, never a client header: prod (TLS-terminated at
        # Vercel) always forces Secure; dev/tests over http stay usable (app_env defaults to dev).
        secure=settings.app_env == "prod",
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
    # deduping so one customer is never listed twice under two formats. last_active is captured
    # from recent_conversations BEFORE the get_or_create bump below, so display stays truthful.
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
        if last_active is not None and last_active_by_phone.get(norm) is None:
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
    # (pre-bump) last_active so the most-recently-active threads are the ones we materialize; every
    # source now supplies a real recency stamp, so no source is artificially forced to sort last.
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
        # Prefer the latest message timestamp (immune to the get_or_create last_active bump);
        # fall back to the pre-bump conversation last_active captured above.
        last_active = recent[-1].created_at if recent else last_active_by_phone.get(norm)
        result.append(
            {"thread_id": thread_id, "phone": norm, "last_active_at": last_active,
             "preview": preview}
        )

    result.sort(key=lambda r: str(r["last_active_at"] or ""), reverse=True)
    return result


@admin_router.get("/conversations/{thread_id}", dependencies=[Depends(require_admin)])
async def get_conversation_thread(thread_id: int) -> list[dict[str, object]]:
    c = get_container()
    # Resolve the conversation's normalized phone from the opaque id; a bad literal id genuinely
    # does not exist -> 404 (distinct from "no messages yet", which is a real empty thread).
    user_id = await c.conversations.get_user_id(thread_id)
    if user_id is None:
        raise HTTPException(status_code=404, detail="thread not found")

    entries: list[dict[str, object]] = []

    for msg in await c.conversations.find_messages_by_user_id(user_id, limit=200):
        entries.append({
            "type": "customer_message" if msg.role == "user" else "ai_reply",
            "timestamp": msg.created_at,
            "text": msg.content,
        })

    # user_id is the normalized E.164 phone; outbound rows are keyed on that same form.
    for row in await c.ingest.find_outbound_by_phone(user_id, limit=200):
        entries.append({
            "type": "template_sent",
            "timestamp": row.created_at,
            "text": f"[{row.state}] {_template_sent_text(row.payload_json)}",
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
    return entries
