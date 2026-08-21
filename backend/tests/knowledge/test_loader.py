import json

from app.knowledge.loader import KINDS, SEEDS_DIR, KnowledgeLoader
from app.store.memory import InMemoryConfigRepo


async def test_get_falls_back_to_seed() -> None:
    loader = KnowledgeLoader(InMemoryConfigRepo(), SEEDS_DIR)
    content = await loader.get("faq")
    assert isinstance(json.loads(content), list)  # seed is a JSON list of {q, a}


async def test_override_wins() -> None:
    repo = InMemoryConfigRepo()
    await repo.set_knowledge_override("brand_voice", "OVERRIDE")
    loader = KnowledgeLoader(repo, SEEDS_DIR)
    assert await loader.get("brand_voice") == "OVERRIDE"


async def test_version_defaults_to_zero() -> None:
    loader = KnowledgeLoader(InMemoryConfigRepo(), SEEDS_DIR)
    assert await loader.knowledge_version() == "0"


async def test_assemble_all_covers_all_kinds() -> None:
    loader = KnowledgeLoader(InMemoryConfigRepo(), SEEDS_DIR)
    parts = await loader.assemble_all()
    assert set(parts) == set(KINDS) and all(parts.values())


def test_all_seed_files_parse() -> None:
    for name in ("faq.json", "business.json", "patterns.json", "size_chart.json"):
        json.loads((SEEDS_DIR / name).read_text(encoding="utf-8"))
    assert (SEEDS_DIR / "brand_voice.md").read_text(encoding="utf-8").strip()


def test_cod_faq_reflects_pin_code_restriction() -> None:
    """Owner-confirmed policy (2026-08-21): COD is PIN-code restricted, matching the Shipping
    Policy ("COD is available only in eligible PIN codes"). This corrects an earlier seed answer
    that wrongly claimed COD had no PIN-code restriction. Guards the seed content so the
    correction isn't silently reverted."""
    faq = json.loads((SEEDS_DIR / "faq.json").read_text(encoding="utf-8"))
    cod_entries = [
        item
        for item in faq
        if "cod" in item["q"].lower() or "cash on delivery" in item["q"].lower()
    ]
    assert cod_entries, "expected a COD FAQ entry"
    answer = cod_entries[0]["a"].lower()
    assert "eligible pin" in answer
    # the outdated "no restriction" wording must be gone
    assert "no pin code restriction" not in answer
    assert "all products" not in answer


def test_faq_covers_courier_delay_and_data_sharing() -> None:
    """New knowledge-gap entries requested 2026-08-21: courier delay disclaimer (from Terms &
    Conditions) and data-sharing scope (from Privacy Policy) were missing from the FAQ the bot
    answers policy questions from. Guards both entries exist with the right substance."""
    faq = json.loads((SEEDS_DIR / "faq.json").read_text(encoding="utf-8"))

    delay_entries = [item for item in faq if "delay" in item["q"].lower()]
    assert delay_entries, "expected a delivery-delay FAQ entry"
    delay_answer = delay_entries[0]["a"].lower()
    assert "courier" in delay_answer
    assert "weather" in delay_answer
    assert "strikes" in delay_answer

    sharing_entries = [item for item in faq if "share" in item["q"].lower()]
    assert sharing_entries, "expected a data-sharing FAQ entry"
    sharing_answer = sharing_entries[0]["a"].lower()
    assert "payment gateway" in sharing_answer
    assert "courier partner" in sharing_answer


def test_business_policy_notes_covers_data_sharing_partners() -> None:
    """Privacy Policy allows sharing personal information with payment gateways, couriers, and
    service providers as necessary; the seed's policy_notes previously only said data is 'used
    to process orders, provide support, and send order updates' without mentioning partner
    sharing. Guards the completed clause (requested 2026-08-21)."""
    business = json.loads((SEEDS_DIR / "business.json").read_text(encoding="utf-8"))
    notes = business["policy_notes"].lower()
    assert "payment gateway" in notes
    assert "courier partner" in notes
