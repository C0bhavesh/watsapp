"""Operational controls document — ADR-002 (kill switch) + ADR-005 (decisions as config).

Each field persists as its OWN plain app_config key so runtime readers (webhook
eligibility, jobs, future outbox drain) keep reading the keys they already use.
"""

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.config.service import ConfigService

_E164_RE = re.compile(r"^\+\d{7,15}$")

REVEAL_ALLOWED: tuple[str, ...] = ("order_number", "email", "status")


class TagLists(BaseModel):
    pending: list[str] = Field(default_factory=list, max_length=10)
    confirmed: list[str] = Field(default_factory=list, max_length=10)
    cancelled: list[str] = Field(default_factory=list, max_length=10)


class AdminControls(BaseModel):
    send_mode: Literal["off", "shadow", "allowlist", "live"] = "off"
    allowlist_phones: list[str] = Field(default_factory=list, max_length=50)
    push_policy: Literal["cod_only", "all", "all_prepaid_no_buttons"] = "cod_only"
    reveal_fields: list[str] = Field(
        default_factory=lambda: ["order_number", "email", "status"], max_length=3
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
    public_base_url: str = Field(default="", max_length=500)
    owner_alert_number: str = Field(default="", max_length=32)

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
_JSON_KEYS: tuple[str, ...] = ("allowlist_phones", "reveal_fields", "tags")


async def load_controls(config: ConfigService) -> AdminControls:
    """Read stored values; anything unset/unparseable falls back to the model default."""
    data: dict[str, object] = {}
    for key in _STR_KEYS:
        value = await config.get_plain(key)
        if value is not None:
            data[key] = value
    staleness = await config.get_plain("push_staleness_hours")
    if staleness is not None and staleness.isdigit():
        data["push_staleness_hours"] = int(staleness)
    for key in _JSON_KEYS:
        raw = await config.get_plain(key)
        if raw is not None:
            try:
                data[key] = json.loads(raw)
            except ValueError:
                pass  # corrupt stored value -> default (never crash the panel)
    return AdminControls.model_validate(data)


async def save_controls(config: ConfigService, controls: AdminControls) -> None:
    for key in _STR_KEYS:
        await config.set_plain(key, getattr(controls, key))
    await config.set_plain("push_staleness_hours", str(controls.push_staleness_hours))
    await config.set_plain("allowlist_phones", json.dumps(controls.allowlist_phones))
    await config.set_plain("reveal_fields", json.dumps(controls.reveal_fields))
    await config.set_plain("tags", json.dumps(controls.tags.model_dump()))
