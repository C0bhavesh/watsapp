"""Kind-specific validation for admin knowledge PUTs.

Stored formats are cafe-loader-compatible: faq/patterns = JSON list, business = JSON
object, brand_voice = raw markdown. A malformed save is impossible: every payload is
validated into these models before serialization.
"""

import json

from pydantic import BaseModel, Field, field_validator


class BrandVoiceBody(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class FaqItem(BaseModel):
    q: str = Field(min_length=1, max_length=2000)
    a: str = Field(min_length=1, max_length=2000)


class FaqBody(BaseModel):
    items: list[FaqItem] = Field(min_length=1, max_length=200)


class PatternItem(BaseModel):
    pattern: str = Field(min_length=1, max_length=100)
    examples: list[str] = Field(min_length=1, max_length=20)
    reply: str = Field(min_length=1, max_length=2000)


class PatternsBody(BaseModel):
    items: list[PatternItem] = Field(min_length=1, max_length=100)


class BusinessBody(BaseModel):
    store_name: str = Field(default="", max_length=2000)
    website: str = Field(default="", max_length=2000)
    instagram: str = Field(default="", max_length=2000)
    support_phone: str = Field(default="", max_length=2000)
    support_email: str = Field(default="", max_length=2000)
    support_hours: str = Field(default="", max_length=2000)
    note: str = Field(default="", max_length=2000)
    extra: dict[str, str] = Field(default_factory=dict, max_length=50)

    @field_validator("extra")
    @classmethod
    def _cap_extra_entry_lengths(cls, value: dict[str, str]) -> dict[str, str]:
        """Bound each entry so a single pair cannot smuggle a megabyte past the pair cap."""
        for key, val in value.items():
            if len(key) > 200 or len(val) > 2000:
                raise ValueError("extra keys must be <=200 and values <=2000 chars")
        return value


def _dump(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def validate_and_serialize(kind: str, payload: dict[str, object]) -> str:
    """Validate *payload* for *kind* and return the canonical stored string.

    Raises pydantic.ValidationError (FastAPI surfaces 422) on any shape problem.
    """
    if kind == "brand_voice":
        return BrandVoiceBody.model_validate(payload).content
    if kind == "faq":
        body = FaqBody.model_validate(payload)
        return _dump([i.model_dump() for i in body.items])
    if kind == "patterns":
        pbody = PatternsBody.model_validate(payload)
        return _dump([i.model_dump() for i in pbody.items])
    if kind == "business":
        return _dump(BusinessBody.model_validate(payload).model_dump())
    raise KeyError(kind)  # guarded by the router's kind check before this call
