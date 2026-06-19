import pytest

from vera_core.config import (
    EnvSecretProvider,
    SecretNotFoundError,
    Settings,
)


def test_defaults_are_local_dev() -> None:
    settings = Settings(_env_file=None)
    assert settings.env == "local"
    assert settings.is_local
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERA_ENV", "staging")
    monkeypatch.setenv("VERA_DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/vera")
    settings = Settings(_env_file=None)
    assert settings.env == "staging"
    assert not settings.is_local
    assert settings.database_url == "postgresql+asyncpg://u:p@db:5432/vera"


def test_env_secret_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERA_SECRET_DB_PASSWORD", "hunter2")
    provider = EnvSecretProvider(prefix="VERA_SECRET_")
    assert provider.get("db-password") == "hunter2"


def test_env_secret_provider_missing() -> None:
    with pytest.raises(SecretNotFoundError):
        EnvSecretProvider().get("definitely-not-set")
