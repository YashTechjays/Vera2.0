"""Unit tests for login provider resolution (sso_provider + platform_login_provider)."""

from types import SimpleNamespace
from typing import cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.providers import (
    resolve_login_provider,
    resolve_platform_login_provider,
)

TENANT = UUID("00000000-0000-0000-0000-0000000000aa")


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object:
        return self._row


class _Session:
    def __init__(self, row: object) -> None:
        self._row = row

    async def execute(self, statement: object) -> _Result:
        return _Result(self._row)


def _session(row: object) -> AsyncSession:
    return cast(AsyncSession, _Session(row))


async def test_returns_provider_when_enabled() -> None:
    row = SimpleNamespace(provider_type="password", enforce_mfa=True)
    provider = await resolve_login_provider(_session(row), TENANT, "password")
    assert provider is not None
    assert provider.provider_type == "password"
    assert provider.enforce_mfa is True


async def test_returns_none_when_no_enabled_provider() -> None:
    provider = await resolve_login_provider(_session(None), TENANT, "password")
    assert provider is None


async def test_platform_returns_provider_when_enabled() -> None:
    row = SimpleNamespace(provider_type="password", enforce_mfa=True)
    provider = await resolve_platform_login_provider(_session(row), "password")
    assert provider is not None
    assert provider.provider_type == "password"
    assert provider.enforce_mfa is True


async def test_platform_returns_none_when_no_enabled_provider() -> None:
    provider = await resolve_platform_login_provider(_session(None), "password")
    assert provider is None
