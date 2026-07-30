from pathlib import Path

from app.store.base import ConfigRepo

KINDS: tuple[str, ...] = ("brand_voice", "faq", "business", "patterns")

_SEED_FILES: dict[str, str] = {
    "brand_voice": "brand_voice.md",
    "faq": "faq.json",
    "business": "business.json",
    "patterns": "patterns.json",
}

SEEDS_DIR: Path = Path(__file__).resolve().parent / "seeds"


class KnowledgeLoader:
    def __init__(self, repo: ConfigRepo, seeds_dir: Path) -> None:
        self._repo = repo
        self._seeds_dir = seeds_dir

    async def get(self, kind: str) -> str:
        override = await self._repo.get_knowledge_override(kind)
        if override is not None:
            return override
        return (self._seeds_dir / _SEED_FILES[kind]).read_text(encoding="utf-8")

    async def knowledge_version(self) -> str:
        return await self._repo.get("knowledge_version") or "0"

    async def assemble_all(self) -> dict[str, str]:
        overrides = await self._repo.get_knowledge_overrides(list(KINDS))
        result: dict[str, str] = {}
        for kind in KINDS:
            value = overrides[kind]
            if value is None:
                value = (self._seeds_dir / _SEED_FILES[kind]).read_text(encoding="utf-8")
            result[kind] = value
        return result
