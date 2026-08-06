"""DPDP retention purge job: disabled at retention_days=0, cutoff-driven when positive."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.admin.controls import AdminControls, save_controls
from app.deps import get_container, reset_container
from app.jobs.retention import run_retention_purge
from app.store.base import DeletionResult

CRON = "topsecret-1234567"  # >= 16 chars (jobs entropy floor)


@pytest.fixture(autouse=True)
def _fresh(master_key: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_MASTER_KEY", master_key)
    monkeypatch.setenv("CRON_SECRET", CRON)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_container()
    yield
    reset_container()


class _SpyIngest:
    def __init__(self) -> None:
        self.cutoff: datetime | None = None
        self.calls = 0

    async def purge_older_than(self, cutoff: datetime) -> DeletionResult:
        self.cutoff = cutoff
        self.calls += 1
        return DeletionResult(order_mappings=3, outbound_messages=1, conversations=0, messages=0)


async def test_disabled_at_zero_does_not_purge() -> None:
    c = get_container()
    spy = _SpyIngest()
    c.ingest = spy  # type: ignore[assignment]
    result = await run_retention_purge(c)
    assert result == {"status": "disabled"}
    assert spy.calls == 0


async def test_enabled_purges_with_cutoff() -> None:
    c = get_container()
    await save_controls(c.config, AdminControls(retention_days=30))
    spy = _SpyIngest()
    c.ingest = spy  # type: ignore[assignment]
    before = datetime.now(UTC)

    result = await run_retention_purge(c)

    assert result["status"] == "purged"
    assert result["retention_days"] == 30
    assert result["deleted"]["order_mappings"] == 3
    assert spy.calls == 1
    expected = before - timedelta(days=30)
    assert spy.cutoff is not None
    assert abs((spy.cutoff - expected).total_seconds()) < 60


async def test_job_registered_and_runs_via_cron_endpoint() -> None:
    from app.main import app as fastapi_app

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/internal/jobs/retention_purge", headers={"X-Cron-Secret": CRON}
        )
    assert resp.status_code == 200
    # Default retention_days=0 -> disabled no-op (no policy invented until Q15 is answered).
    assert resp.json() == {"job": "retention_purge", "result": {"status": "disabled"}}
