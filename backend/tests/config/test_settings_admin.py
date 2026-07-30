import pytest

from app.config.settings import Settings


def test_admin_password_defaults_empty(master_key: str) -> None:
    s = Settings(app_master_key=master_key, _env_file=None)  # type: ignore[call-arg]
    assert s.admin_password == ""


def test_admin_password_reads_env(monkeypatch: pytest.MonkeyPatch, master_key: str) -> None:
    monkeypatch.setenv("ADMIN_PASSWORD", "s3cret")
    s = Settings(app_master_key=master_key, _env_file=None)  # type: ignore[call-arg]
    assert s.admin_password == "s3cret"
