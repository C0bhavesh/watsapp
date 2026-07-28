"""Apply app/store/schema.sql to DATABASE_URL. Run: python -m scripts.apply_schema"""

import asyncio
import pathlib

import asyncpg

from app.config.settings import Settings


async def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set")
    sql = (pathlib.Path(__file__).parent.parent / "app" / "store" / "schema.sql").read_text()
    conn = await asyncpg.connect(settings.database_url)
    try:
        await conn.execute(sql)
        print("schema applied OK")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
