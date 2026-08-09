"""Deterministic confirm/cancel button dispatch — the mutation-safety core (ADR-004).

Every test proves an invariant: the LLM is never involved (the container has no provider),
ownership is re-checked live before any mutation, cancel is two-phase, and no handler exception
escapes. Sends are captured (no real Meta call); Shopify is a fake that records mutations.
"""

from dataclasses import dataclass

import pytest

from app.channels.copy import copy_for
from app.channels.whatsapp_inbound import InboundButton, InboundInteractive
from app.channels.whatsapp_sender import SendResult
from app.config.crypto import SecretVault
from app.config.service import ConfigService
from app.core import order_actions
from app.core.order_actions import dispatch_button
from app.shopify.errors import ShopifyGraphQLError, ShopifyUnavailable
from app.shopify.models import CancelRequested, Order
from app.store.memory import InMemoryConfigRepo, InMemoryIngestStore

OWNER = "919664290413"
OWNER_E164 = "+919664290413"
GID = "gid://shopify/Order/1"


def _order(
    gid: str = GID,
    phone: str = OWNER_E164,
    tags: tuple[str, ...] = (),
    cancelled_at: str | None = None,
    fulfillment_status: str | None = None,
    locale: str | None = None,
) -> Order:
    return Order(
        gid=gid, name="tavas1", email=None, phone=phone, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=fulfillment_status,
        cancelled_at=cancelled_at, tags=tags, payment_gateway_names=(), total=None,
        customer_locale=locale,
    )


class FakeShopify:
    def __init__(
        self,
        order: Order | None = None,
        get_raises: Exception | None = None,
        add_tags_raises: Exception | None = None,
        cancel_raises: Exception | None = None,
    ) -> None:
        self._order = order
        self.get_raises = get_raises
        self.add_tags_raises = add_tags_raises
        self.cancel_raises = cancel_raises
        self.get_calls: list[str] = []
        self.add_tags_calls: list[tuple[str, list[str]]] = []
        self.cancel_calls: list[str] = []

    async def get_order(self, gid: str) -> Order | None:
        self.get_calls.append(gid)
        if self.get_raises:
            raise self.get_raises
        return self._order if (self._order is not None and self._order.gid == gid) else None

    async def add_tags(self, auth: object, tags: object) -> None:
        self.add_tags_calls.append((auth.order.gid, list(tags)))  # type: ignore[attr-defined]
        if self.add_tags_raises:
            raise self.add_tags_raises

    async def cancel_order(
        self, auth: object, *, reason: str = "CUSTOMER", restock: bool = True
    ) -> CancelRequested:
        self.cancel_calls.append(auth.order.gid)  # type: ignore[attr-defined]
        if self.cancel_raises:
            raise self.cancel_raises
        return CancelRequested(job_id="gid://shopify/Job/1")


@dataclass
class FakeContainer:
    config: ConfigService
    http: object
    shopify: FakeShopify
    ingest: InMemoryIngestStore


class Sends:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.buttons: list[tuple[str, str, list[tuple[str, str]]]] = []

    @property
    def last_text(self) -> str:
        return self.texts[-1][1]


@pytest.fixture
def sends(monkeypatch: pytest.MonkeyPatch) -> Sends:
    captured = Sends()

    async def _send_text(http, cfg, to, body, timeout=20.0) -> SendResult:
        captured.texts.append((to, body))
        return SendResult(ok=True, status_code=200, wamid="w", error=None)

    async def _send_buttons(http, cfg, to, body_text, buttons, timeout=20.0) -> SendResult:
        captured.buttons.append((to, body_text, list(buttons)))
        return SendResult(ok=True, status_code=200, wamid="w", error=None)

    monkeypatch.setattr(order_actions, "send_text", _send_text)
    monkeypatch.setattr(order_actions, "send_buttons", _send_buttons)
    return captured


async def _container(master_key: str, shopify: FakeShopify) -> FakeContainer:
    config = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    await config.set_secret("whatsapp:access_token", "tok")
    await config.set_secret("whatsapp:app_secret", "sec")
    await config.set_secret("whatsapp:verify_token", "ver")
    await config.set_plain("whatsapp:phone_number_id", "1298805403309058")
    await config.set_plain("whatsapp:waba_id", "2454816495000045")
    await config.set_plain("whatsapp:api_version", "v23.0")
    return FakeContainer(
        config=config, http=object(), shopify=shopify, ingest=InMemoryIngestStore()
    )


def _button(payload: str, wa_id: str = OWNER, mid: str = "m1") -> InboundButton:
    return InboundButton(
        message_id=mid, wa_id=wa_id, payload=payload, button_text="",
        context_message_id=None, timestamp="",
    )


def _interactive(button_id: str, wa_id: str = OWNER, mid: str = "m1") -> InboundInteractive:
    return InboundInteractive(
        message_id=mid, wa_id=wa_id, button_id=button_id, button_title="", timestamp="",
    )


# --- ownership / refusal (no mutation, no leak) ---

async def test_non_owner_refused_no_mutation_no_leak(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order(phone="+911111111111"))  # belongs to a different number
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert shopify.add_tags_calls == []
    assert shopify.cancel_calls == []
    assert c.ingest.order_actions == []
    assert sends.last_text == copy_for("not_found", "en")
    assert GID not in sends.last_text and "tavas1" not in sends.last_text


async def test_unknown_gid_refused(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=None)  # get_order returns None
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert shopify.add_tags_calls == [] and shopify.cancel_calls == []
    assert c.ingest.order_actions == []
    assert sends.last_text == copy_for("not_found", "en")


# --- confirm ---

async def test_confirm_tags_records_and_status(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order())
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert shopify.add_tags_calls == [(GID, ["confirmed"])]
    action = c.ingest.order_actions[-1]
    assert action["action"] == "confirm"
    assert action["result"] == "ok"
    assert action["actor_wa_id"] == OWNER
    assert action["source_wamid"] == "m1"
    assert c.ingest._mapping_status[GID] == "confirmed"
    assert sends.last_text == copy_for("confirm_success", "en")


async def test_confirm_idempotent_on_already_confirmed(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order(tags=("confirmed",)))
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert shopify.add_tags_calls == []  # no second mutation
    assert c.ingest.order_actions == []
    assert sends.last_text == copy_for("already_confirmed", "en")


async def test_confirm_on_cancelled_order_says_already_cancelled(
    master_key: str, sends: Sends
) -> None:
    shopify = FakeShopify(order=_order(cancelled_at="2026-08-10T00:00:00Z"))
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert shopify.add_tags_calls == []
    assert sends.last_text == copy_for("already_cancelled", "en")


# --- cancel: two-phase ---

async def test_cancel_first_tap_sends_buttons_no_mutation(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order())
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:cancel:{GID}"))
    assert shopify.cancel_calls == []  # NEVER cancelled on the first tap
    assert shopify.add_tags_calls == []
    assert len(sends.buttons) == 1
    _to, body_text, buttons = sends.buttons[0]
    assert body_text == copy_for("cancel_are_you_sure", "en")
    assert [b[0] for b in buttons] == [
        f"order:cancel:confirm:{GID}", f"order:cancel:abort:{GID}"
    ]
    assert [b[1] for b in buttons] == [
        copy_for("cancel_yes_title", "en"), copy_for("cancel_no_title", "en")
    ]


async def test_cancel_refused_when_dispatched(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order(fulfillment_status="FULFILLED"))
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:cancel:{GID}"))
    assert shopify.cancel_calls == []
    assert sends.buttons == []
    assert sends.last_text == copy_for("cancel_too_late", "en")


async def test_cancel_confirm_cancels_tags_and_status(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order())
    c = await _container(master_key, shopify)
    await dispatch_button(c, _interactive(f"order:cancel:confirm:{GID}"))
    assert shopify.cancel_calls == [GID]
    assert shopify.add_tags_calls == [(GID, ["bot-cancel-requested"])]  # provisional tag
    assert c.ingest._mapping_status[GID] == "cancel_requested"
    assert await c.ingest.orders_awaiting_cancel_reconcile() == [GID]
    action = c.ingest.order_actions[-1]
    assert action["action"] == "cancel_requested"
    assert action["result"] == "ok"
    assert sends.last_text == copy_for("cancel_requested", "en")


async def test_cancel_confirm_refused_when_dispatched(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order(fulfillment_status="FULFILLED"))
    c = await _container(master_key, shopify)
    await dispatch_button(c, _interactive(f"order:cancel:confirm:{GID}"))
    assert shopify.cancel_calls == []
    assert sends.last_text == copy_for("cancel_too_late", "en")


async def test_cancel_confirm_idempotent_when_provisional_tag_present(
    master_key: str, sends: Sends
) -> None:
    # Order already carries the provisional tag: the async orderCancel has not reflected as
    # cancelledAt yet, but a re-tap must NOT fire a second orderCancel.
    shopify = FakeShopify(order=_order(tags=("bot-cancel-requested",)))
    c = await _container(master_key, shopify)
    await dispatch_button(c, _interactive(f"order:cancel:confirm:{GID}"))
    assert shopify.cancel_calls == []
    assert sends.last_text == copy_for("cancel_requested", "en")


async def test_cancel_abort_no_mutation(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order())
    c = await _container(master_key, shopify)
    await dispatch_button(c, _interactive(f"order:cancel:abort:{GID}"))
    assert shopify.cancel_calls == []
    assert shopify.add_tags_calls == []
    assert c.ingest.order_actions == []
    assert sends.last_text == copy_for("cancel_kept", "en")


async def test_cancel_confirm_user_errors_records_and_replies_failed(
    master_key: str, sends: Sends
) -> None:
    err = ShopifyGraphQLError(["Order has been fulfilled and cannot be cancelled"], ("FULFILLED",))
    shopify = FakeShopify(order=_order(), cancel_raises=err)
    c = await _container(master_key, shopify)
    await dispatch_button(c, _interactive(f"order:cancel:confirm:{GID}"))
    assert shopify.cancel_calls == [GID]
    assert shopify.add_tags_calls == []  # provisional tag NOT applied on failure
    assert c.ingest._mapping_status.get(GID) is None  # status NOT advanced
    action = c.ingest.order_actions[-1]
    assert action["action"] == "cancel_requested"
    assert action["result"] == "error"
    assert action["user_errors_json"] is not None
    assert "fulfilled" in action["user_errors_json"].lower()
    assert sends.last_text == copy_for("cancel_failed", "en")


# --- garbage / errors never escape ---

async def test_garbage_payload_safe_reply_no_resolve_no_mutation(
    master_key: str, sends: Sends
) -> None:
    shopify = FakeShopify(order=_order())
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button("totally:bogus:payload"))
    assert shopify.get_calls == []  # never even resolved
    assert shopify.cancel_calls == [] and shopify.add_tags_calls == []
    assert sends.last_text == copy_for("error_fallback", "en")


async def test_shopify_error_degrades_to_fallback_no_escape(
    master_key: str, sends: Sends
) -> None:
    shopify = FakeShopify(order=_order(), add_tags_raises=ShopifyUnavailable("down"))
    c = await _container(master_key, shopify)
    # Must not raise — the webhook has to still ack 200.
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert sends.last_text == copy_for("error_fallback", "en")
    assert c.ingest.order_actions == []  # tag failed -> nothing recorded/advanced
    assert c.ingest._mapping_status.get(GID) is None


# --- language ---

async def test_reply_uses_order_language(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order(locale="hi-IN"))
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert sends.last_text == copy_for("confirm_success", "hi")
