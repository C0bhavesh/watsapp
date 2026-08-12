"""Deterministic confirm/cancel button dispatch — the mutation-safety core (ADR-004).

Every test proves an invariant: the LLM is never involved (the container has no provider),
ownership is re-checked live before any mutation, cancel is two-phase, and no handler exception
escapes. Sends are captured (no real Meta call); Shopify is a fake that records mutations.
"""

import asyncio
import json
from dataclasses import dataclass, replace

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
        cancel_gate: asyncio.Event | None = None,
    ) -> None:
        self._order = order
        self.get_raises = get_raises
        self.add_tags_raises = add_tags_raises
        self.cancel_raises = cancel_raises
        self.cancel_gate = cancel_gate
        self.get_calls: list[str] = []
        self.add_tags_calls: list[tuple[str, list[str]]] = []
        self.cancel_calls: list[str] = []

    async def get_order(self, gid: str) -> Order | None:
        self.get_calls.append(gid)
        if self.get_raises:
            raise self.get_raises
        return self._order if (self._order is not None and self._order.gid == gid) else None

    async def add_tags(self, auth: object, tags: object) -> None:
        gid = auth.order.gid  # type: ignore[attr-defined]
        tag_list = list(tags)  # type: ignore[call-overload]
        self.add_tags_calls.append((gid, tag_list))
        if self.add_tags_raises:
            raise self.add_tags_raises
        # Real Shopify tagsAdd is synchronous: reflect the tag onto the stored order so a later
        # live re-fetch (a racing second tap) sees it and the cancel idempotency check fires.
        if self._order is not None and self._order.gid == gid:
            new_tags = self._order.tags + tuple(t for t in tag_list if t not in self._order.tags)
            self._order = replace(self._order, tags=new_tags)

    async def cancel_order(
        self, auth: object, *, reason: str = "CUSTOMER", restock: bool = True
    ) -> CancelRequested:
        self.cancel_calls.append(auth.order.gid)  # type: ignore[attr-defined]
        # Only the FIRST cancel blocks (simulating an in-flight async orderCancel), letting a
        # racing second tap run to completion while the first is still mid-flight.
        if self.cancel_gate is not None and len(self.cancel_calls) == 1:
            await self.cancel_gate.wait()
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


async def _container(
    master_key: str,
    shopify: FakeShopify,
    *,
    send_mode: str = "live",
    allowlist: list[str] | None = None,
) -> FakeContainer:
    config = ConfigService(InMemoryConfigRepo(), SecretVault(master_key))
    await config.set_secret("whatsapp:access_token", "tok")
    await config.set_secret("whatsapp:app_secret", "sec")
    await config.set_secret("whatsapp:verify_token", "ver")
    await config.set_plain("whatsapp:phone_number_id", "1298805403309058")
    await config.set_plain("whatsapp:waba_id", "2454816495000045")
    await config.set_plain("whatsapp:api_version", "v23.0")
    # The button-tap kill switch reads these. Existing tests default to "live" (full behavior);
    # shadow/allowlist tests pass an explicit mode so a suppressed tap has NO live effect.
    await config.set_plain("send_mode", send_mode)
    if allowlist is not None:
        await config.set_plain("allowlist_phones", json.dumps(allowlist))
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


async def test_dispatch_reads_live_shopify_not_the_stale_mirror(
    master_key: str, sends: Sends
) -> None:
    # Mutation-safety regression guard (Critical Rule 3): order_actions / resolve_by_gid must read
    # LIVE Shopify, NEVER the database mirror. Seed the mirror with a STALE copy (not cancelled, no
    # cancel-requested tag) while live Shopify reports the SAME gid already cancelled. Reading live
    # Shopify makes the cancel-confirm tap reply already-cancelled and fire ZERO mutations. If a
    # future change repointed resolve_by_gid at MirrorOrderSource, the stale mirror copy would look
    # cancellable and this dispatch would fire a real orderCancel -- this test would then fail (an
    # empty mirror would silently miss and fall through to Shopify, catching nothing).
    live_cancelled = _order(cancelled_at="2026-08-10T00:00:00Z")
    shopify = FakeShopify(order=live_cancelled)
    c = await _container(master_key, shopify)
    stale_mirror = _order(cancelled_at=None, tags=())  # not cancelled; no cancel-requested tag
    await c.ingest.upsert_order_mirror(stale_mirror)

    await dispatch_button(c, _interactive(f"order:cancel:confirm:{GID}"))

    assert shopify.get_calls == [GID]  # proves the LIVE re-fetch happened
    assert shopify.cancel_calls == []  # zero mutation despite the stale, cancellable-looking mirror
    assert shopify.add_tags_calls == []
    assert c.ingest.order_actions == []
    assert sends.last_text == copy_for("already_cancelled", "en")


async def test_unknown_gid_refused(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=None)  # get_order returns None
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert shopify.add_tags_calls == [] and shopify.cancel_calls == []
    assert c.ingest.order_actions == []
    assert sends.last_text == copy_for("not_found", "en")


# --- kill switch (ADR-002): shadow / allowlist gate a tap the same way the drain gates a push ---

async def test_shadow_mode_confirm_suppressed_no_mutation_no_send(
    master_key: str, sends: Sends
) -> None:
    shopify = FakeShopify(order=_order())
    c = await _container(master_key, shopify, send_mode="shadow")
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert shopify.get_calls == []  # suppressed BEFORE the live re-fetch -> nothing leaks
    assert shopify.add_tags_calls == []
    assert c.ingest.order_actions == []
    assert sends.texts == [] and sends.buttons == []


async def test_shadow_mode_cancel_confirm_suppressed_no_cancel_no_send(
    master_key: str, sends: Sends
) -> None:
    shopify = FakeShopify(order=_order())
    c = await _container(master_key, shopify, send_mode="shadow")
    await dispatch_button(c, _interactive(f"order:cancel:confirm:{GID}"))
    assert shopify.cancel_calls == []
    assert shopify.get_calls == []
    assert c.ingest.order_actions == []
    assert sends.texts == [] and sends.buttons == []


async def test_allowlist_miss_suppressed_no_mutation_no_send(
    master_key: str, sends: Sends
) -> None:
    shopify = FakeShopify(order=_order())
    c = await _container(
        master_key, shopify, send_mode="allowlist", allowlist=["+911111111111"]
    )
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert shopify.add_tags_calls == []
    assert shopify.get_calls == []
    assert c.ingest.order_actions == []
    assert sends.texts == [] and sends.buttons == []


async def test_allowlist_hit_full_confirm_behavior(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order())
    c = await _container(master_key, shopify, send_mode="allowlist", allowlist=[OWNER_E164])
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert shopify.add_tags_calls == [(GID, ["confirmed"])]
    assert c.ingest._mapping_status[GID] == "confirmed"
    assert sends.last_text == copy_for("confirm_success", "en")


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
    # FINDING 4: state is advanced BEFORE the (async) orderCancel, so the provisional tag + status
    # are already applied when the cancel then fails. The order stays cancel_requested (reconcile
    # leaves it alone until cancelledAt appears); the customer is told cancel_failed.
    assert shopify.add_tags_calls == [(GID, ["bot-cancel-requested"])]
    assert c.ingest._mapping_status.get(GID) == "cancel_requested"
    action = c.ingest.order_actions[-1]
    assert action["action"] == "cancel_requested"
    assert action["result"] == "error"
    assert action["user_errors_json"] is not None
    assert "fulfilled" in action["user_errors_json"].lower()
    assert sends.last_text == copy_for("cancel_failed", "en")


async def test_cancel_confirm_race_two_taps_cancel_at_most_once(
    master_key: str, sends: Sends
) -> None:
    # Two racing cancel-confirm taps (distinct message-ids). The first advances state (writes the
    # provisional tag) BEFORE its orderCancel, so the second tap's live re-fetch — which runs while
    # the first cancel is still in flight — sees the marker and refuses a duplicate orderCancel.
    gate = asyncio.Event()
    shopify = FakeShopify(order=_order(), cancel_gate=gate)
    c = await _container(master_key, shopify)
    first = asyncio.create_task(
        dispatch_button(c, _interactive(f"order:cancel:confirm:{GID}", mid="m1"))
    )
    while not shopify.cancel_calls:  # let the first tap's orderCancel become "in flight"
        await asyncio.sleep(0)
    # The second tap runs to completion while the first cancel is still blocked on the gate.
    await dispatch_button(c, _interactive(f"order:cancel:confirm:{GID}", mid="m2"))
    gate.set()
    await first
    assert shopify.cancel_calls == [GID]  # cancelled at most once despite two taps
    assert sends.last_text == copy_for("cancel_requested", "en")  # 2nd tap: already requested


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
    # FINDING 3 (audit symmetry): a FAILED confirm mutation is still audited, like a success —
    # every ATTEMPTED mutation leaves an order_actions row.
    action = c.ingest.order_actions[-1]
    assert action["action"] == "confirm"
    assert action["result"] == "error"
    assert action["user_errors_json"] is None  # transport error carries no structured detail
    assert c.ingest._mapping_status.get(GID) is None  # status NOT advanced on failure


async def test_confirm_graphql_error_records_error_row_with_detail(
    master_key: str, sends: Sends
) -> None:
    err = ShopifyGraphQLError(["tag limit exceeded"], ("TOO_MANY_TAGS",))
    shopify = FakeShopify(order=_order(), add_tags_raises=err)
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    action = c.ingest.order_actions[-1]
    assert action["action"] == "confirm"
    assert action["result"] == "error"
    assert action["user_errors_json"] is not None
    assert "tag limit" in action["user_errors_json"].lower()
    assert sends.last_text == copy_for("error_fallback", "en")


async def test_cancel_confirm_transport_error_records_error_row(
    master_key: str, sends: Sends
) -> None:
    # A transport (non-GraphQL) ShopifyError on the cancel mutation must ALSO be audited — the
    # cancel handler only catches GraphQL, so this lands in the outer handler, which records it.
    shopify = FakeShopify(order=_order(), cancel_raises=ShopifyUnavailable("network down"))
    c = await _container(master_key, shopify)
    await dispatch_button(c, _interactive(f"order:cancel:confirm:{GID}"))
    assert shopify.cancel_calls == [GID]  # the mutation was attempted
    action = c.ingest.order_actions[-1]
    assert action["action"] == "cancel_requested"
    assert action["result"] == "error"
    assert sends.last_text == copy_for("error_fallback", "en")


# --- language ---

async def test_reply_uses_order_language(master_key: str, sends: Sends) -> None:
    shopify = FakeShopify(order=_order(locale="hi-IN"))
    c = await _container(master_key, shopify)
    await dispatch_button(c, _button(f"order:confirm:{GID}"))
    assert sends.last_text == copy_for("confirm_success", "hi")
