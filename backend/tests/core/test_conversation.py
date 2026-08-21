"""Unit tests for the order-name recovery fallback in the conversation pipeline.

Covers ``_recover_order_by_name`` -- the helper that lets a customer be helped by the order
number they type, whether or not their WhatsApp number is already mapped to an order.
Ownership is enforced by ``resolve_by_order_name`` (returning None both for "no such order" and
"not this sender's order"), so these tests pin: an owned token is recovered, an unowned/absent
token reveals nothing, extraction is safe on huge/hostile input, and a number-shaped token of
the wrong digit count never reaches Shopify at all -- it produces a format hint instead.
"""

import logging

import pytest

from app.admin.controls import AdminControls
from app.agents.base import AgentReply
from app.channels.whatsapp_inbound import InboundText
from app.core.conversation import (
    _agent_reply,
    _extract_order_number_candidate,
    _recover_order_by_name,
)
from app.deps import get_container, reset_container
from app.shopify.models import ORDER_NUMBER_DIGIT_LENGTH, Order
from app.store.base import MappingView


def _order(gid: str, name: str, phone: str | None) -> Order:
    return Order(
        gid=gid, name=name, email=None, phone=phone, shipping_phone=None,
        billing_phone=None, financial_status=None, fulfillment_status=None,
        cancelled_at=None, tags=(), payment_gateway_names=(), total=None,
        customer_locale=None,
    )


class _FakeShopify:
    """Only ``find_order_by_name`` is exercised by the recovery path; the rest satisfy the
    structural ``OrderSource`` shape without being called."""

    def __init__(self, orders_by_name: dict[str, Order] | None = None) -> None:
        self.orders_by_name = orders_by_name or {}
        self.calls: list[str] = []

    async def get_order(self, gid: str) -> Order | None:
        return None

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        self.calls.append(raw_name)
        return self.orders_by_name.get(raw_name)

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        return []


async def test_recover_order_by_name_returns_owned_order() -> None:
    shopify = _FakeShopify(
        orders_by_name={"tavas5432": _order("gid://5", "tavas5432", "+919999999999")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "hi where is tavas5432")

    assert len(orders) == 1
    assert orders[0].order.gid == "gid://5"
    assert orders[0].verified_phone == "+919999999999"
    assert hint is None


async def test_recover_order_by_name_unowned_returns_empty() -> None:
    # The order exists but belongs to a different phone -> resolve_by_order_name returns None,
    # so nothing is revealed (and the None is indistinguishable from "no such order").
    shopify = _FakeShopify(
        orders_by_name={"tavas6543": _order("gid://6", "tavas6543", "+911111111111")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "where is tavas6543")

    assert orders == []
    assert hint is None


async def test_recover_order_by_name_no_token_returns_empty() -> None:
    shopify = _FakeShopify(
        orders_by_name={"tavas5432": _order("gid://5", "tavas5432", "+919999999999")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "where is my order please")

    assert orders == []
    assert hint is None


async def test_recover_order_by_name_extracts_token_from_long_hostile_text() -> None:
    shopify = _FakeShopify(
        orders_by_name={"tavas5432": _order("gid://5", "tavas5432", "+919999999999")}
    )
    hostile = (
        "\U0001f600" * 5000 + "\n\x00 TAVAS no-digits here " + "tavas5432"
        + " ' \" ; -- OR 1=1 " + "力" * 5000
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", hostile)

    assert len(orders) == 1
    assert orders[0].order.gid == "gid://5"
    assert hint is None


async def test_recover_order_by_name_bare_digits_normalize_to_tavas_prefix() -> None:
    # A bare 4-digit number (no "tavas"/"#" prefix) is still a valid candidate -- passed through
    # as-is to resolve_by_order_name, which normalizes it exactly like the real Shopify client
    # does (bare digits -> "tavas" + digits).
    shopify = _FakeShopify(
        orders_by_name={"9652": _order("gid://9652", "tavas9652", "+919999999999")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "my order number is 9652")

    assert len(orders) == 1
    assert orders[0].order.gid == "gid://9652"
    assert hint is None


async def test_recover_order_by_name_hash_prefixed() -> None:
    shopify = _FakeShopify(
        orders_by_name={"#9652": _order("gid://9652", "tavas9652", "+919999999999")}
    )

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "order #9652 please")

    assert len(orders) == 1
    assert hint is None


async def test_recover_order_by_name_short_bare_number_is_not_a_candidate() -> None:
    # Below the 3-digit floor for a bare (unprefixed) number -- dates/quantities/etc. should not
    # be mistaken for an order-number attempt.
    shopify = _FakeShopify()

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "I ordered 2 shirts")

    assert orders == []
    assert hint is None
    assert shopify.calls == []


async def test_recover_order_by_name_wrong_digit_count_never_calls_shopify() -> None:
    shopify = _FakeShopify()

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "my order id is 965")

    assert orders == []
    assert hint is not None
    assert str(ORDER_NUMBER_DIGIT_LENGTH) in hint
    assert shopify.calls == []


async def test_recover_order_by_name_tavas_prefixed_wrong_digit_count_never_calls_shopify() -> None:
    shopify = _FakeShopify()

    orders, hint = await _recover_order_by_name(shopify, "919999999999", "where is tavas96522")

    assert orders == []
    assert hint is not None
    assert shopify.calls == []


async def test_recover_order_by_name_logs_a_warning_for_a_one_digit_longer_candidate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 5 digits == ORDER_NUMBER_DIGIT_LENGTH + 1 -- the leading indicator the store's order
    # numbers may have grown past the configured length.
    shopify = _FakeShopify()

    with caplog.at_level(logging.WARNING, logger="app.core.conversation"):
        orders, hint = await _recover_order_by_name(shopify, "919999999999", "order 96522")

    assert orders == []
    assert hint is not None
    assert "one more than ORDER_NUMBER_DIGIT_LENGTH" in caplog.text


@pytest.mark.parametrize("text", ["order 965", "order 9652222"])
async def test_recover_order_by_name_does_not_log_for_other_wrong_lengths(
    text: str, caplog: pytest.LogCaptureFixture
) -> None:
    # 3 digits and 7 digits are both wrong, but neither is "exactly one more" -- only that
    # specific case is the drift signal worth logging.
    shopify = _FakeShopify()

    with caplog.at_level(logging.WARNING, logger="app.core.conversation"):
        orders, hint = await _recover_order_by_name(shopify, "919999999999", text)

    assert orders == []
    assert hint is not None
    assert "one more than ORDER_NUMBER_DIGIT_LENGTH" not in caplog.text


def test_extract_order_number_candidate_prefers_the_correct_length_over_a_pincode() -> None:
    candidate = _extract_order_number_candidate("my pin is 400001 and my order is 9652")
    assert candidate == "9652"


def test_extract_order_number_candidate_prefers_the_correct_length_over_a_phone_number() -> None:
    candidate = _extract_order_number_candidate("call me on 9876543210 about order 9652")
    assert candidate == "9652"


def test_extract_order_number_candidate_tavas_prefix_wins_regardless_of_position() -> None:
    # A bare 4-digit run ("9876") appears BEFORE the tavas-prefixed token in the text, and both
    # are the same digit count -- only the tavas/#/bare priority order (not the length filter)
    # can explain the tavas-prefixed token winning here.
    candidate = _extract_order_number_candidate("call 9876 about tavas5432 please")
    assert candidate == "tavas5432"


def test_extract_order_number_candidate_no_candidate_returns_none() -> None:
    assert _extract_order_number_candidate("where is my order please") is None


class _FakeMirrorIngestFull:
    """Implements every IngestStore method _agent_reply's own code path touches directly
    (count_orders_by_phone) plus what resolve_by_phone/MirrorOrderSource need."""

    def __init__(
        self,
        mappings: list[MappingView] | None = None,
        mirrored_order: Order | None = None,
    ) -> None:
        self.mappings = mappings or []
        self.mirrored_order = mirrored_order
        self.mirror_calls: list[str] = []

    async def find_mappings_by_phone(self, phone_e164: str, limit: int = 20) -> list[MappingView]:
        return [m for m in self.mappings if m.phone_e164 == phone_e164][:limit]

    async def count_orders_by_phone(self, phone_e164: str) -> int:
        return len([m for m in self.mappings if m.phone_e164 == phone_e164])

    async def get_mirrored_order(self, gid: str) -> Order | None:
        self.mirror_calls.append("get_mirrored_order")
        return self.mirrored_order

    async def find_mirrored_order_by_name(self, raw_name: str) -> Order | None:
        self.mirror_calls.append("find_mirrored_order_by_name")
        return None

    async def find_mirrored_orders_by_phone(self, phone_e164: str) -> list[Order]:
        self.mirror_calls.append("find_mirrored_orders_by_phone")
        return []


class _PoisonedShopify:
    """Raises if the Q&A path ever falls through to it -- proves the mirror served the hit
    without touching Shopify at all."""

    async def get_order(self, gid: str) -> Order | None:
        raise AssertionError("Shopify.get_order must not be called on a mirror hit")

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        return None

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        return []


class _FakeLiveShopify:
    """Answers ``get_order``/``find_order_by_name`` directly and records every call it
    receives -- the mirror-image of ``_PoisonedShopify`` above. Used by the ``exchange``-intent
    test to prove order resolution hit Shopify directly (never ``MirrorOrderSource``):
    ``check_exchange_eligibility`` depends on ``Fulfillment.delivered_at``, which only a genuine
    live Shopify read ever populates -- the mirror-ingestion path never sets it."""

    def __init__(self, order: Order) -> None:
        self._order = order
        self.calls: list[str] = []

    async def get_order(self, gid: str) -> Order | None:
        self.calls.append("get_order")
        return self._order if gid == self._order.gid else None

    async def find_order_by_name(self, raw_name: str) -> Order | None:
        self.calls.append("find_order_by_name")
        return None

    async def find_customer_orders_by_phone(self, phone_e164: str) -> list[Order]:
        self.calls.append("find_customer_orders_by_phone")
        return []


async def test_agent_reply_order_tracking_reads_from_mirror_not_shopify(
    monkeypatch, master_key: str
) -> None:
    order = _order("gid://1", "tavas3733", "+919999999999")
    mapping = MappingView(
        order_gid="gid://1", order_name="tavas3733", phone_e164="+919999999999",
        status="pending", is_cod=False, created_at=None,
    )
    ingest = _FakeMirrorIngestFull(mappings=[mapping], mirrored_order=order)
    shopify = _PoisonedShopify()

    # get_container() builds Settings() from env; APP_MASTER_KEY is required (no DATABASE_URL
    # -> the in-memory container, whose ingest/shopify we then monkeypatch). Same bootstrap the
    # webhook tests use (tests/test_whatsapp_webhook.py).
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    c = get_container()
    monkeypatch.setattr(c, "ingest", ingest)
    monkeypatch.setattr(c, "shopify", shopify)

    captured: dict[str, object] = {}

    async def fake_classify_intent(*args: object, **kwargs: object) -> str:
        return "order_tracking"

    async def fake_assemble_all(self: object) -> dict[str, str]:
        return {}

    async def fake_run_agent(context: object, intent: str, container: object) -> AgentReply:
        captured["orders"] = context.orders  # type: ignore[attr-defined]
        return AgentReply(text="ok", handoff=False)

    monkeypatch.setattr("app.core.conversation.classify_intent", fake_classify_intent)
    monkeypatch.setattr(
        "app.core.conversation.KnowledgeLoader.assemble_all", fake_assemble_all
    )
    monkeypatch.setattr("app.core.conversation._run_agent", fake_run_agent)

    # The message carries the order token so BOTH mirror reads run: resolve_by_phone -> the
    # mirror's get_mirrored_order, and _recover_order_by_name -> resolve_by_order_name -> the
    # mirror's find_mirrored_order_by_name. _PoisonedShopify.get_order raises (proving the phone
    # path was served from the mirror), while its find_order_by_name is a benign None so the
    # name path may fall through past the mirror miss without exploding the test.
    event = InboundText(
        message_id="wamid.1", wa_id="919999999999", text="where is my order tavas3733",
        timestamp="1699999999",
    )

    await _agent_reply(
        c, event, [], "+919999999999", False, (object(), "model", "key", None), AdminControls()
    )

    resolved = captured["orders"]
    assert len(resolved) == 1  # type: ignore[arg-type]
    assert resolved[0].order.gid == "gid://1"  # type: ignore[index]
    assert ingest.mirror_calls == ["get_mirrored_order", "find_mirrored_order_by_name"]


async def test_agent_reply_exchange_intent_resolves_orders_and_exchange_requests(
    monkeypatch, master_key: str
) -> None:
    """The exchange intent must get context.orders AND context.exchange_requests populated,
    AND -- unlike order_tracking -- must resolve orders via a genuine live Shopify call, never
    MirrorOrderSource: check_exchange_eligibility depends on Fulfillment.delivered_at, which the
    mirror-ingestion path never populates (only a real live Shopify GraphQL read does)."""
    order = _order("gid://1", "tavas3733", "+919999999999")
    mapping = MappingView(
        order_gid="gid://1", order_name="tavas3733", phone_e164="+919999999999",
        status="pending", is_cod=False, created_at=None,
    )
    ingest = _FakeMirrorIngestFull(mappings=[mapping], mirrored_order=order)
    shopify = _FakeLiveShopify(order)

    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    c = get_container()
    monkeypatch.setattr(c, "ingest", ingest)
    monkeypatch.setattr(c, "shopify", shopify)
    # "vertex" is an env-auth provider (no stored key needed) -- see test_deps.py's
    # test_active_llm_env_provider_needs_no_stored_key for the same pattern -- so active_llm
    # below resolves to a real, usable tuple without a fake key ever entering the config store.
    await c.config.set_plain("llm:active_provider", "vertex")

    from app.core.exchange_models import ExchangeRequest

    existing = ExchangeRequest(
        id=1, order_gid="gid://1", order_name="tavas3733", phone_e164="+919999999999",
        requested_size="M", status="requested", requested_at="2026-08-20T00:00:00+00:00",
        return_tracking_url=None, replacement_tracking_url=None,
        updated_at="2026-08-20T00:00:00+00:00",
    )

    class _FakeExchanges:
        async def list_for_phone(self, phone_e164: str) -> list[ExchangeRequest]:
            return [existing] if phone_e164 == "+919999999999" else []

    monkeypatch.setattr(c, "exchanges", _FakeExchanges())

    captured: dict[str, object] = {}

    async def fake_classify_intent(*args: object, **kwargs: object) -> str:
        return "exchange"

    async def fake_assemble_all(self: object) -> dict[str, str]:
        return {}

    async def fake_run_agent(context: object, intent: str, container: object) -> AgentReply:
        captured["orders"] = context.orders  # type: ignore[attr-defined]
        captured["exchange_requests"] = context.exchange_requests  # type: ignore[attr-defined]
        return AgentReply(text="ok", handoff=False)

    monkeypatch.setattr("app.core.conversation.classify_intent", fake_classify_intent)
    monkeypatch.setattr(
        "app.core.conversation.KnowledgeLoader.assemble_all", fake_assemble_all
    )
    monkeypatch.setattr("app.core.conversation._run_agent", fake_run_agent)

    from app.deps import active_llm

    llm = await active_llm(c.settings, c.config)
    assert llm is not None
    event = InboundText(
        message_id="wamid.2", wa_id="919999999999", text="I want to exchange this",
        timestamp="1699999999",
    )

    await _agent_reply(c, event, [], "+919999999999", False, llm, AdminControls())

    assert len(captured["orders"]) == 1  # type: ignore[arg-type]
    assert captured["orders"][0].order.name == "tavas3733"  # type: ignore[index]
    assert captured["exchange_requests"] == [existing]
    # The correction under test: a genuine live Shopify call happened (get_order), and the
    # mirror's own read methods were never touched at all.
    assert "get_order" in shopify.calls
    assert ingest.mirror_calls == []


async def test_agent_reply_exchange_intent_survives_exchange_store_failure(
    monkeypatch, master_key: str
) -> None:
    """A transient exchange-store failure on the LIVE customer path must NOT crash the turn:
    the exchange lookup degrades to "no existing exchange requests" (empty list) and the agent
    still runs, matching the admin thread-detail guard's catch-log-degrade shape."""
    order = _order("gid://1", "tavas3733", "+919999999999")
    mapping = MappingView(
        order_gid="gid://1", order_name="tavas3733", phone_e164="+919999999999",
        status="pending", is_cod=False, created_at=None,
    )
    ingest = _FakeMirrorIngestFull(mappings=[mapping], mirrored_order=order)
    shopify = _FakeLiveShopify(order)

    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    reset_container()
    c = get_container()
    monkeypatch.setattr(c, "ingest", ingest)
    monkeypatch.setattr(c, "shopify", shopify)
    await c.config.set_plain("llm:active_provider", "vertex")

    from app.core.exchange_models import ExchangeRequest

    class _BoomExchanges:
        async def list_for_phone(self, phone_e164: str) -> list[ExchangeRequest]:
            raise RuntimeError("exchange_requests table does not exist")

    monkeypatch.setattr(c, "exchanges", _BoomExchanges())

    captured: dict[str, object] = {}

    async def fake_classify_intent(*args: object, **kwargs: object) -> str:
        return "exchange"

    async def fake_assemble_all(self: object) -> dict[str, str]:
        return {}

    async def fake_run_agent(context: object, intent: str, container: object) -> AgentReply:
        captured["exchange_requests"] = context.exchange_requests  # type: ignore[attr-defined]
        return AgentReply(text="ok", handoff=False)

    monkeypatch.setattr("app.core.conversation.classify_intent", fake_classify_intent)
    monkeypatch.setattr(
        "app.core.conversation.KnowledgeLoader.assemble_all", fake_assemble_all
    )
    monkeypatch.setattr("app.core.conversation._run_agent", fake_run_agent)

    from app.deps import active_llm

    llm = await active_llm(c.settings, c.config)
    assert llm is not None
    event = InboundText(
        message_id="wamid.3", wa_id="919999999999", text="I want to exchange this",
        timestamp="1699999999",
    )

    # Must NOT raise: the store blew up but the turn completes with an empty-list fallback.
    reply, _handoff = await _agent_reply(c, event, [], "+919999999999", False, llm, AdminControls())

    assert reply == "ok"
    assert captured["exchange_requests"] == []
