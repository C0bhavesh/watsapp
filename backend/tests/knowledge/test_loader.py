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
    for name in ("faq.json", "business.json", "patterns.json"):
        json.loads((SEEDS_DIR / name).read_text(encoding="utf-8"))
    assert (SEEDS_DIR / "brand_voice.md").read_text(encoding="utf-8").strip()
