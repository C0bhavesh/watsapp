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


def test_cod_faq_reflects_all_products_no_pin_restriction() -> None:
    """Owner-confirmed policy (2026-08-21): COD is available on all products with NO PIN-code
    restriction. This supersedes the old seed answer ("only in eligible PIN codes"), which was
    outdated/wrong. Guards the seed content so the correction isn't silently reverted."""
    faq = json.loads((SEEDS_DIR / "faq.json").read_text(encoding="utf-8"))
    cod_entries = [
        item
        for item in faq
        if "cod" in item["q"].lower() or "cash on delivery" in item["q"].lower()
    ]
    assert cod_entries, "expected a COD FAQ entry"
    answer = cod_entries[0]["a"].lower()
    assert "all products" in answer
    assert "no pin" in answer
    # the outdated PIN-code restriction wording must be gone
    assert "eligible pin" not in answer
