"""The one-time backfill must refuse to run without a real database.

get_container() falls back to InMemoryIngestStore when settings.database_url is empty, so an
unguarded run would page a year of orders out of Shopify into a process-local dict, throw it
away at exit, and print "backfill complete" -- a success report for a no-op. scripts/
apply_schema.py already fails fast on the same condition; this mirrors it.
"""

import pytest

from scripts.backfill_orders import main


async def test_backfill_refuses_to_run_without_a_database_url(
    master_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.setenv("DATABASE_URL", "")
    called: list[str] = []
    monkeypatch.setattr(
        "scripts.backfill_orders.get_container",
        lambda: called.append("built"),  # type: ignore[arg-type, return-value]
    )

    with pytest.raises(SystemExit) as exc:
        await main()

    assert "DATABASE_URL" in str(exc.value)
    # Fails BEFORE any container/Shopify work happens.
    assert called == []
