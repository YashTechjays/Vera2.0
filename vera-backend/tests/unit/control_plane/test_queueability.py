"""ensure_queueable — the enqueue-time gate: a form must be dialable before it may queue."""

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.exceptions import CustomAPIException
from control_plane.queueability import IN_FLIGHT_FORM_STATUSES, ensure_queueable, ensure_va_capacity
from vera_core.config.kms import KeyManagementService
from vera_core.models import PatientForm, Tenant


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


class _CountResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _CapacitySession:
    """First execute() is the advisory lock, second is the in-flight count."""

    def __init__(self, in_flight: int) -> None:
        self._in_flight = in_flight
        self.executed: list[Any] = []

    async def execute(self, stmt: Any) -> _CountResult:
        self.executed.append(stmt)
        return _CountResult(self._in_flight)


def _capacity_tenant(limit: int) -> Tenant:
    return cast(Tenant, SimpleNamespace(id=uuid4(), max_agents_per_va=limit))


@pytest.mark.asyncio
async def test_below_limit_passes() -> None:
    session = _CapacitySession(in_flight=2)
    await ensure_va_capacity(cast(AsyncSession, session), _capacity_tenant(3), uuid4())  # no raise
    assert len(session.executed) == 2  # advisory lock, then count


@pytest.mark.asyncio
async def test_at_limit_raises_conflict_with_actionable_data() -> None:
    session = _CapacitySession(in_flight=3)
    with pytest.raises(CustomAPIException) as exc:
        await ensure_va_capacity(cast(AsyncSession, session), _capacity_tenant(3), uuid4())
    assert "concurrent-agent limit (3)" in str(exc.value.message)
    assert exc.value.data == {"limit": 3, "in_flight": 3}


@pytest.mark.asyncio
async def test_over_limit_raises() -> None:
    # Defensive: a count already past the limit (e.g. limit was lowered) still gates.
    session = _CapacitySession(in_flight=5)
    with pytest.raises(CustomAPIException):
        await ensure_va_capacity(cast(AsyncSession, session), _capacity_tenant(3), uuid4())


def test_in_flight_statuses_are_queue_plus_active() -> None:
    assert set(IN_FLIGHT_FORM_STATUSES) == {"in_queue", "in_call", "ai_processing"}


@pytest.mark.asyncio
async def test_browser_callee_allows_a_missing_trunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("control_plane.queueability.get_integration_credentials", _creds_missing)
    await ensure_queueable(
        cast(AsyncSession, _FakeSession()),
        cast(KeyManagementService, object()),
        _form("+15551234567"),
        browser_callee=True,
    )


@pytest.mark.asyncio
async def test_browser_callee_still_requires_e164(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("control_plane.queueability.get_integration_credentials", _creds_missing)
    with pytest.raises(CustomAPIException) as exc:
        await ensure_queueable(
            cast(AsyncSession, _FakeSession()),
            cast(KeyManagementService, object()),
            _form("555-1234"),
            browser_callee=True,
        )
    assert "phone" in str(exc.value.message).lower()
