"""Unit tests for QueueDispatcher.

Uses an in-memory approach: the dispatcher is tested through its public
interface with mock SQLAlchemy session results and a FakeLiveKit, verifying
FIFO ordering, concurrency gating, working-hours checks, and expiry.

The dial-path tests below route `try_dispatch`'s `session.execute()` calls to
canned results through `FakeSession`, which inspects each statement's target
entity (Tenant / the count aggregate / PatientForm-with-LIMIT vs
PatientForm-without / InsuranceProvider) rather than assuming a fixed call
order — this survives try_dispatch's queries being reordered.
"""

from collections import deque
from datetime import time
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.config.kms import KeyManagementService
from vera_core.db import uuid7
from vera_core.models import Call, CallEvent, InsuranceProvider, PatientForm, Tenant
from vera_core.models.enums import CallStatus, FormStatus
from vera_core.services import queue_dispatcher
from vera_core.services.queue_dispatcher import is_within_working_hours, try_dispatch
from vera_core.telephony import OutboundDialError


class TestIsWithinWorkingHours:
    """Working-hours gate — pure function, no DB."""

    def _provider(
        self,
        start: time | None = None,
        end: time | None = None,
    ) -> MagicMock:
        p = MagicMock()
        p.working_hour_start = start
        p.working_hour_end = end
        return p

    def test_none_hours_means_always_available(self) -> None:
        assert is_within_working_hours(self._provider()) is True

    @patch("vera_core.services.queue_dispatcher._now_eastern_time")
    def test_within_hours(self, mock_now: MagicMock) -> None:
        mock_now.return_value = time(10, 0)
        provider = self._provider(start=time(8, 0), end=time(17, 0))
        assert is_within_working_hours(provider) is True

    @patch("vera_core.services.queue_dispatcher._now_eastern_time")
    def test_outside_hours(self, mock_now: MagicMock) -> None:
        mock_now.return_value = time(6, 0)
        provider = self._provider(start=time(8, 0), end=time(17, 0))
        assert is_within_working_hours(provider) is False

    @patch("vera_core.services.queue_dispatcher._now_eastern_time")
    def test_at_boundary_start(self, mock_now: MagicMock) -> None:
        mock_now.return_value = time(8, 0)
        provider = self._provider(start=time(8, 0), end=time(17, 0))
        assert is_within_working_hours(provider) is True

    @patch("vera_core.services.queue_dispatcher._now_eastern_time")
    def test_at_boundary_end(self, mock_now: MagicMock) -> None:
        mock_now.return_value = time(17, 0)
        provider = self._provider(start=time(8, 0), end=time(17, 0))
        assert is_within_working_hours(provider) is True


# ---------------------------------------------------------------------------
# Dial-path fakes: a minimal AsyncSession stand-in + a FakeLiveKit gateway.
# ---------------------------------------------------------------------------


class _Result:
    """Stand-in for a SQLAlchemy `Result` — only the accessors try_dispatch calls."""

    def __init__(self, *, scalar: Any = None, rows: list[Any] | None = None) -> None:
        self._scalar = scalar
        self._rows = rows if rows is not None else []

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def scalar_one(self) -> Any:
        return self._scalar

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self._rows


def _bound_value(stmt: Any, column_name: str) -> Any:
    """Pull a bound literal (e.g. `InsuranceProvider.name == "Acme"`) out of a
    statement's WHERE clause by column name — lets the fake resolve which
    provider a `select(InsuranceProvider).where(...)` is asking for."""
    where = stmt.whereclause
    clauses = where.clauses if hasattr(where, "clauses") else [where]
    for clause in clauses:
        if getattr(clause.left, "name", None) == column_name:
            return clause.right.value
    return None


class _NestedTransaction:
    """Stand-in for `session.begin_nested()` — a plain pass-through async
    context manager; the fake session has nothing to roll back."""

    async def __aenter__(self) -> "_NestedTransaction":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class FakeSession:
    """Routes try_dispatch's `execute()` calls to canned results by the
    statement's target entity — Tenant, the count aggregate (no entity),
    PatientForm with a LIMIT (candidates) vs without (expired), and
    InsuranceProvider by name. Order-independent by construction."""

    def __init__(
        self,
        *,
        tenant: Tenant,
        active_count: int = 0,
        candidates: list[PatientForm] | None = None,
        expired: list[PatientForm] | None = None,
        providers: dict[str, InsuranceProvider] | None = None,
    ) -> None:
        self.tenant = tenant
        self.active_count = active_count
        self.candidates = candidates or []
        self.expired = expired or []
        self.providers = providers or {}
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Tenant:
            return _Result(scalar=self.tenant)
        if entity is InsuranceProvider:
            name = _bound_value(stmt, "name")
            return _Result(scalar=self.providers.get(name))
        if entity is PatientForm:
            rows = self.candidates if stmt._limit_clause is not None else self.expired
            return _Result(rows=rows)
        # select(func.count()) — no mapped entity.
        return _Result(scalar=self.active_count)

    def add(self, obj: Any) -> None:
        if hasattr(obj, "id") and obj.id is None:
            obj.id = uuid7()
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction()

    def calls_added(self) -> list[Call]:
        return [o for o in self.added if isinstance(o, Call)]

    def call_events_added(self) -> list[CallEvent]:
        return [o for o in self.added if isinstance(o, CallEvent)]


class FakeLiveKit:
    """Minimal LiveKitGateway stand-in — records room creation + SIP dials."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.dispatch_metadata: list[dict[str, object] | None] = []
        self.sip_dials: list[tuple[str, str, str]] = []
        self.deleted: list[str] = []
        self.dial_error = False

    async def create_call_room(
        self, room_name: str, metadata: dict[str, object] | None = None
    ) -> None:
        self.created.append(room_name)
        self.dispatch_metadata.append(metadata)

    async def create_sip_participant(
        self, room_name: str, phone_number: str, trunk_id: str
    ) -> None:
        if self.dial_error:
            raise OutboundDialError("fake provider rejected the call")
        self.sip_dials.append((room_name, phone_number, trunk_id))

    async def delete_room(self, room_name: str) -> None:
        self.deleted.append(room_name)


def _tenant(**overrides: Any) -> Tenant:
    defaults: dict[str, Any] = {
        "id": uuid7(),
        "name": "Test Tenant",
        "slug": f"tenant-{uuid7().hex[:8]}",
        "status": "active",
        "max_agents_per_va": 3,
        "max_retries": 5,
        "queue_expiry_hours": 48,
        "persona_tweak": {},
    }
    defaults.update(overrides)
    return Tenant(**defaults)


def _form(tenant_id: Any, **overrides: Any) -> PatientForm:
    defaults: dict[str, Any] = {
        "id": uuid7(),
        "tenant_id": tenant_id,
        "schema_version_id": uuid7(),
        "status": FormStatus.IN_QUEUE.value,
        "patient_name": "Jane Doe",
        "insurance_provider_phone_number": "+15551234567",
        "insurance_provider": None,
        "retry_count": 0,
        "enqueued_by_id": None,
        "ivr_navigation_enabled": True,
    }
    defaults.update(overrides)
    return PatientForm(**defaults)


@pytest.fixture(autouse=True)
def _stub_credentials(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, Any] | None]:
    """Default: a trunk is configured. Individual tests override `creds["value"]`."""
    creds: dict[str, dict[str, Any] | None] = {"value": {"trunk_id": "ST_test"}}

    async def _fake_get_integration_credentials(
        session: Any, kms: Any, *, integration_type_name: str
    ) -> dict[str, Any] | None:
        return creds["value"]

    monkeypatch.setattr(
        queue_dispatcher, "get_integration_credentials", _fake_get_integration_credentials
    )
    return creds


async def _dispatch(session: FakeSession, tenant_id: Any, livekit: FakeLiveKit) -> int:
    """try_dispatch(), casting the fakes to their real types — mypy-strict clean,
    mirroring the `cast(AsyncSession, fake)` convention used elsewhere in the
    unit-test suite (e.g. tests/unit/control_plane/test_queueability.py)."""
    return await try_dispatch(
        cast(AsyncSession, session),
        tenant_id,
        livekit,
        cast(KeyManagementService, object()),
    )


async def test_dispatch_dials_the_forms_payer_number(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    tenant = _tenant(persona_tweak={"greeting": "Custom greeting"})
    form = _form(tenant.id, ivr_navigation_enabled=True)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 1
    call = session.calls_added()[0]
    room_name = livekit.created[0]
    assert livekit.sip_dials == [(room_name, "+15551234567", "ST_test")]
    assert call.current_status == CallStatus.INITIATED.value

    metadata = livekit.dispatch_metadata[0]
    assert metadata is not None
    assert metadata["wait_for_speaker"] is True
    assert metadata["publish_events"] is True
    assert metadata["enable_ivr_navigation"] is True
    assert metadata["persona_tweak"] == {"greeting": "Custom greeting"}


async def test_ivr_navigation_key_absent_when_form_opts_out(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    tenant = _tenant()
    form = _form(tenant.id, ivr_navigation_enabled=False)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 1
    metadata = livekit.dispatch_metadata[0]
    assert metadata is not None
    assert "enable_ivr_navigation" not in metadata
    assert "persona_tweak" not in metadata  # tenant.persona_tweak is empty


async def test_dispatch_without_trunk_leaves_forms_queued(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    _stub_credentials["value"] = None
    tenant = _tenant()
    form = _form(tenant.id)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 0
    assert form.status == FormStatus.IN_QUEUE.value
    assert livekit.created == []
    assert livekit.sip_dials == []


async def test_dial_failure_marks_call_failed_and_requeues_form(
    _stub_credentials: dict[str, dict[str, Any] | None],
) -> None:
    tenant = _tenant()
    form = _form(tenant.id, retry_count=0)
    session = FakeSession(tenant=tenant, candidates=[form])
    livekit = FakeLiveKit()
    livekit.dial_error = True

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 0
    call = session.calls_added()[0]
    assert call.current_status == CallStatus.FAILED.value
    events = session.call_events_added()
    assert any(e.event_value == CallStatus.FAILED.value for e in events)
    assert livekit.deleted == livekit.created  # the room was torn down
    assert form.status == FormStatus.IN_QUEUE.value
    assert form.retry_count == 1


async def test_dials_are_paced_one_second_apart(
    _stub_credentials: dict[str, dict[str, Any] | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = _tenant(max_agents_per_va=5)
    form_a = _form(tenant.id)
    form_b = _form(tenant.id)
    session = FakeSession(tenant=tenant, candidates=[form_a, form_b])
    livekit = FakeLiveKit()

    sleeps: deque[float] = deque()

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(queue_dispatcher.asyncio, "sleep", _fake_sleep)  # type: ignore[attr-defined]

    dispatched = await _dispatch(session, tenant.id, livekit)

    assert dispatched == 2
    assert list(sleeps) == [1.0]
