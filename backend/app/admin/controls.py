"""Operational controls document — ADR-002 (kill switch) + ADR-005 (decisions as config).

Each field persists as its OWN plain app_config key so runtime readers (webhook
eligibility, jobs, future outbox drain) keep reading the keys they already use.
"""

import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config.service import ConfigService

# ASCII digits only: \d is Unicode-aware, so Arabic-Indic/fullwidth digit strings would pass
# a \d pattern yet are not real phone numbers — pin to [0-9]. `\Z` (not `$`) anchors the true
# end of the string: Python's `$` also matches just before a trailing "\n", so a value like
# "+919111111111\n" would slip past a `$`-anchored pattern.
_E164_RE = re.compile(r"^\+[0-9]{7,15}\Z")

# "items" reverses the earlier client decision that order items/amounts stay hidden (recorded
# in docs/superpowers/specs/2026-08-10-order-item-details-design.md) -- default-enabled per
# that decision, still independently admin-toggleable like every other value here.
# "tracking" surfaces the Shopify courier/tracking link when the order is shipped (Q10, already
# answered) -- pre-approved, default-enabled, still admin-toggleable.
REVEAL_ALLOWED: tuple[str, ...] = ("order_number", "email", "status", "items", "tracking")

# Per-tag cap: Shopify tags max out at 255 chars, so an over-long tag would fail at runtime
# inside tagsAdd — reject it at the edge instead.
_Tag = Annotated[str, Field(max_length=64)]


class TagLists(BaseModel):
    pending: list[_Tag] = Field(default_factory=list, max_length=10)
    confirmed: list[_Tag] = Field(default_factory=list, max_length=10)
    cancelled: list[_Tag] = Field(default_factory=list, max_length=10)
    # Provisional tag written when a cancel is REQUESTED (before Shopify confirms cancelledAt);
    # the final `cancelled` tag is applied by the reconcile job once cancellation is confirmed.
    # Defaults so an older stored `tags` JSON without this key validates (migration-safe).
    cancel_requested: list[_Tag] = Field(
        default_factory=lambda: ["bot-cancel-requested"], max_length=10
    )


class AdminControls(BaseModel):
    send_mode: Literal["off", "shadow", "allowlist", "live"] = "off"
    allowlist_phones: list[str] = Field(default_factory=list, max_length=50)
    push_policy: Literal["cod_only", "all", "all_prepaid_no_buttons"] = "cod_only"
    reveal_fields: list[str] = Field(
        default_factory=lambda: ["order_number", "email", "status", "items", "tracking"],
        max_length=5,
    )
    tags: TagLists = Field(
        default_factory=lambda: TagLists(
            pending=["COD pending"],
            confirmed=["confirmed"],
            cancelled=["cancelled"],
        )
    )
    default_language: Literal["en", "hi", "gu"] = "en"
    push_staleness_hours: int = Field(default=6, ge=1, le=168)
    # DPDP retention window in days. 0 = disabled (no automatic deletion) — the safe default;
    # the exact period is a pending client decision (Q15), so nothing is purged until an owner
    # sets a positive value. le=3650 caps it at ~10 years.
    retention_days: int = Field(default=0, ge=0, le=3650)
    public_base_url: str = Field(default="", max_length=500)
    owner_alert_number: str = Field(default="", max_length=32)
    # VIP/repeat-customer threshold (order count, not spend — order_mappings has no amount
    # column; total_spend is a rare on-demand live Shopify lookup, never pre-computed).
    vip_order_count_threshold: int = Field(default=3, ge=1, le=100)

    @field_validator("allowlist_phones")
    @classmethod
    def _phones_e164(cls, v: list[str]) -> list[str]:
        for p in v:
            if not _E164_RE.match(p):
                raise ValueError(f"not E.164 (+countrycode...): {p!r}")
        return v

    @field_validator("reveal_fields")
    @classmethod
    def _reveal_subset(cls, v: list[str]) -> list[str]:
        bad = [f for f in v if f not in REVEAL_ALLOWED]
        if bad:
            raise ValueError(f"unknown reveal fields: {bad}")
        return v

    @field_validator("public_base_url")
    @classmethod
    def _https_or_empty(cls, v: str) -> str:
        if v and not v.startswith("https://"):
            raise ValueError("public_base_url must be https:// (or empty)")
        return v.rstrip("/")

    @field_validator("owner_alert_number")
    @classmethod
    def _owner_number(cls, v: str) -> str:
        if v and not _E164_RE.match(v):
            raise ValueError("owner_alert_number must be E.164 (+countrycode...) or empty")
        return v


_STR_KEYS: tuple[str, ...] = (
    "send_mode",
    "push_policy",
    "default_language",
    "public_base_url",
    "owner_alert_number",
)
_INT_KEYS: tuple[str, ...] = ("push_staleness_hours", "retention_days", "vip_order_count_threshold")
_JSON_KEYS: tuple[str, ...] = ("allowlist_phones", "reveal_fields", "tags")


async def load_controls(config: ConfigService) -> AdminControls:
    """Read stored values; anything unset/unparseable falls back to the model default."""
    data: dict[str, object] = {}
    for key in _STR_KEYS:
        value = await config.get_plain(key)
        if value is not None:
            data[key] = value
    for key in _INT_KEYS:
        raw_int = await config.get_plain(key)
        # Require ASCII digits, not str.isdigit()/int() alone. int("٣٠") == 30, so a bare int()
        # (or a plain isdigit(), which is Unicode-aware) would silently accept an Arabic-Indic
        # stored value and enable an unintended retention period; str.isascii() AND str.isdigit()
        # is what closes it. A non-matching value is left unset -> the field's own model default.
        if raw_int is not None and raw_int.isascii() and raw_int.isdigit():
            data[key] = int(raw_int)
    for key in _JSON_KEYS:
        raw = await config.get_plain(key)
        if raw is not None:
            try:
                data[key] = json.loads(raw)
            except ValueError:
                pass  # corrupt stored value -> default (never crash the panel)
    return _validate_per_field(data)


def _validate_per_field(data: dict[str, object]) -> AdminControls:
    """Validate the assembled dict, defaulting ONLY the field(s) that fail — never the whole doc.

    Validating the model as a whole and falling back to ``AdminControls()`` on any error means one
    corrupt field silently resets every other (e.g. an unrelated bad value re-widening a
    deliberately narrowed ``reveal_fields`` back to the PII-exposing default). Instead, drop the
    top-level keys pydantic reports as invalid and re-validate, so each good field is preserved.
    """
    try:
        return AdminControls.model_validate(data)
    except ValidationError as exc:
        bad_keys = {loc[0] for loc in (e["loc"] for e in exc.errors()) if loc}
        clean = {k: v for k, v in data.items() if k not in bad_keys}
        try:
            return AdminControls.model_validate(clean)
        except ValidationError:
            # Should not happen (only known-valid keys remain), but never brick the panel.
            return AdminControls()


async def save_controls(config: ConfigService, controls: AdminControls) -> None:
    for key in _STR_KEYS:
        await config.set_plain(key, getattr(controls, key))
    for key in _INT_KEYS:
        await config.set_plain(key, str(getattr(controls, key)))
    await config.set_plain("allowlist_phones", json.dumps(controls.allowlist_phones))
    await config.set_plain("reveal_fields", json.dumps(controls.reveal_fields))
    await config.set_plain("tags", json.dumps(controls.tags.model_dump()))
