"""ensure_queueable — the enqueue-time gate: a form must be dialable before it may queue."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.exceptions import CustomAPIException
from control_plane.queueability import ensure_queueable
from vera_core.config.kms import KeyManagementService
from vera_core.models import PatientForm


class _FakeSession:  # ensure_queueable only passes the session through to the creds lookup
    pass


def _form(phone: str | None) -> PatientForm:
    # ensure_queueable only reads one attribute — a SimpleNamespace stands in for the
    # real ORM model, cast at the call site so this stays strict-mypy clean.
    return cast(PatientForm, SimpleNamespace(insurance_provider_phone_number=phone))


async def _creds_present(
    session: AsyncSession, kms: KeyManagementService, *, integration_type_name: str
) -> dict[str, Any]:
    assert integration_type_name == "livekit_outbound_trunk_id"
    return {"trunk_id": "ST_trunk"}


async def _creds_missing(
    session: AsyncSession, kms: KeyManagementService, *, integration_type_name: str
) -> dict[str, Any] | None:
    return None


@pytest.mark.asyncio
async def test_rejects_missing_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("control_plane.queueability.get_integration_credentials", _creds_present)
    with pytest.raises(CustomAPIException) as exc:
        await ensure_queueable(
            cast(AsyncSession, _FakeSession()), cast(KeyManagementService, object()), _form(None)
        )
    assert "phone" in str(exc.value.message).lower()


@pytest.mark.asyncio
async def test_rejects_non_e164_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("control_plane.queueability.get_integration_credentials", _creds_present)
    with pytest.raises(CustomAPIException):
        await ensure_queueable(
            cast(AsyncSession, _FakeSession()),
            cast(KeyManagementService, object()),
            _form("555-1234"),
        )


@pytest.mark.asyncio
async def test_rejects_when_trunk_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("control_plane.queueability.get_integration_credentials", _creds_missing)
    with pytest.raises(CustomAPIException) as exc:
        await ensure_queueable(
            cast(AsyncSession, _FakeSession()),
            cast(KeyManagementService, object()),
            _form("+15551234567"),
        )
    assert "trunk" in str(exc.value.message).lower()


@pytest.mark.asyncio
async def test_accepts_dialable_form(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("control_plane.queueability.get_integration_credentials", _creds_present)
    await ensure_queueable(
        cast(AsyncSession, _FakeSession()),
        cast(KeyManagementService, object()),
        _form("+15551234567"),
    )  # no raise
