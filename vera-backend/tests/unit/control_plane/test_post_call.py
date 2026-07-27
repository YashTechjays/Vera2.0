"""Post-call AI-processing resolution — the system edge out of AI_PROCESSING.

After a completed call parks the form in AI_PROCESSING, `resolve_ai_processing`
decides the diagrammed system transition: low completion + retries remaining →
auto-requeue (IN_QUEUE); otherwise → EXCEPTION_REVIEW for human review. COMPLETED
is never reachable from here — only a reviewer's manual approve sets it.

The auto-retry edge is feature-gated (`auto_retry_enabled`, default OFF): until a
post-call form-filling mechanism exists, completion never improves between calls,
so a retry would redial to no benefit — everything goes to EXCEPTION_REVIEW.

DB seam faked exactly like `test_worker_events.py`: `tenant_session` is
monkeypatched to a `_FakeSession` routed by target entity.
"""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import control_plane.post_call as post_call
from control_plane.post_call import resolve_ai_processing, sweep_stuck_ai_processing
from vera_core.audit import AuditRecord
from vera_core.models import Call, PatientForm, Tenant
from vera_core.models.audit_log import AuditEvent
from vera_core.models.enums import CallStatus, FormStatus, ReviewReason
from vera_core.observability.correlation import RoomRef

# The tenant_session seam is monkeypatched, so the sessionmaker is never touched.
_SM = cast("async_sessionmaker[AsyncSession]", object())


class _Result:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        return self._value

    def scalars(self) -> "_Result":
        return self

    def all(self) -> Any:
        return self._value


class _FakeSession:
    """Routes `execute()` by the statement's target entity."""

    def __init__(self, *, call: Any = None, form: Any = None, tenant: Any = None) -> None:
        self.call = call
        self.form = form
        self.tenant = tenant

    async def execute(self, stmt: Any) -> _Result:
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Call:
            # Scalar-column selects (Call.id lists for the sweeper) vs row selects.
            if stmt.column_descriptions[0]["name"] == "id":
                return _Result([self.call.id] if self.call is not None else [])
            return _Result(self.call)
        if entity is PatientForm:
            return _Result(self.form)
        if entity is Tenant:
            return _Result(self.tenant)
        raise AssertionError(f"unexpected query entity {entity}")


class _FakeSessionCtx:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _SpyAudit:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def emit(self, record: AuditRecord) -> None:
        self.records.append(record)


def _wire(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> _SpyAudit:
    monkeypatch.setattr(post_call, "tenant_session", lambda sm, tid: _FakeSessionCtx(session))
    return _SpyAudit()


def _tenant(tenant_id: UUID, **overrides: Any) -> Tenant:
    defaults: dict[str, Any] = {
        "id": tenant_id,
        "name": "Test Tenant",
        "slug": f"tenant-{uuid4().hex[:8]}",
        "status": "active",
        "max_agents_per_va": 3,
        "max_retries": 3,
        "retry_fill_threshold": 0.95,
        "queue_expiry_hours": 48,
        "persona_tweak": {},
    }
    defaults.update(overrides)
    return Tenant(**defaults)


def _call_row(tenant_id: UUID, call_id: UUID, form_id: UUID, **overrides: Any) -> Call:
    defaults: dict[str, Any] = {
        "id": call_id,
        "tenant_id": tenant_id,
        "form_id": form_id,
        "current_status": CallStatus.COMPLETED.value,
    }
    defaults.update(overrides)
    return Call(**defaults)


def _form_row(tenant_id: UUID, form_id: UUID, **overrides: Any) -> PatientForm:
    defaults: dict[str, Any] = {
        "id": form_id,
        "tenant_id": tenant_id,
        "schema_version_id": uuid4(),
        "status": FormStatus.AI_PROCESSING.value,
        "patient_name": "Jane Doe",
        "insurance_provider_phone_number": "+15551234567",
        "retry_count": 0,
        "completion_pct": 100.0,
        "enqueued_at": None,
    }
    defaults.update(overrides)
    return PatientForm(**defaults)


def _ids() -> tuple[UUID, UUID, UUID, RoomRef]:
    tenant_id, call_id, form_id = uuid4(), uuid4(), uuid4()
    return tenant_id, call_id, form_id, RoomRef(tenant_id=tenant_id, call_id=call_id)


@pytest.mark.asyncio
async def test_high_completion_moves_form_to_exception_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, call_id, form_id, ref = _ids()
    form = _form_row(tenant_id, form_id, completion_pct=100.0)
    session = _FakeSession(
        call=_call_row(tenant_id, call_id, form_id),
        form=form,
        tenant=_tenant(tenant_id),
    )
    audit = _wire(monkeypatch, session)

    requeued = await resolve_ai_processing(_SM, audit, ref, trigger="call.ended")

    assert requeued is False
    assert form.status == FormStatus.EXCEPTION_REVIEW.value
    assert form.retry_count == 0
    # This synchronous fallback never ran the AI eval — the reviewer must see WHY.
    assert form.review_reason == ReviewReason.NOT_EVALUATED.value
    assert len(audit.records) == 1
    record = audit.records[0]
    assert record.event_type == AuditEvent.FORM_STATUS_CHANGE.value
    assert record.detail["from"] == FormStatus.AI_PROCESSING.value
    assert record.detail["to"] == FormStatus.EXCEPTION_REVIEW.value
    assert record.detail["call_id"] == str(call_id)


@pytest.mark.asyncio
async def test_low_completion_auto_requeues_while_retries_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, call_id, form_id, ref = _ids()
    form = _form_row(tenant_id, form_id, completion_pct=40.0, retry_count=0)
    session = _FakeSession(
        call=_call_row(tenant_id, call_id, form_id),
        form=form,
        tenant=_tenant(tenant_id, max_retries=3, retry_fill_threshold=0.95),
    )
    audit = _wire(monkeypatch, session)

    requeued = await resolve_ai_processing(
        _SM, audit, ref, trigger="call.ended", auto_retry_enabled=True
    )

    assert requeued is True
    assert form.status == FormStatus.IN_QUEUE.value
    assert form.retry_count == 1
    assert form.enqueued_at is not None  # DB-clock stamp set by the resolver
    assert audit.records[0].detail["to"] == FormStatus.IN_QUEUE.value


@pytest.mark.asyncio
async def test_low_completion_with_auto_retry_disabled_goes_to_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (flag OFF): low completion still goes to EXCEPTION_REVIEW and the
    retry budget is untouched — no form-filling mechanism exists yet, so a retry
    call could never raise completion."""
    tenant_id, call_id, form_id, ref = _ids()
    form = _form_row(tenant_id, form_id, completion_pct=40.0, retry_count=0)
    session = _FakeSession(
        call=_call_row(tenant_id, call_id, form_id),
        form=form,
        tenant=_tenant(tenant_id, max_retries=3, retry_fill_threshold=0.95),
    )
    audit = _wire(monkeypatch, session)

    requeued = await resolve_ai_processing(_SM, audit, ref, trigger="call.ended")

    assert requeued is False
    assert form.status == FormStatus.EXCEPTION_REVIEW.value
    assert form.retry_count == 0
    assert audit.records[0].detail["to"] == FormStatus.EXCEPTION_REVIEW.value


@pytest.mark.asyncio
async def test_low_completion_with_retries_exhausted_goes_to_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, call_id, form_id, ref = _ids()
    form = _form_row(tenant_id, form_id, completion_pct=40.0, retry_count=3)
    session = _FakeSession(
        call=_call_row(tenant_id, call_id, form_id),
        form=form,
        tenant=_tenant(tenant_id, max_retries=3),
    )
    audit = _wire(monkeypatch, session)

    requeued = await resolve_ai_processing(
        _SM, audit, ref, trigger="call.ended", auto_retry_enabled=True
    )

    assert requeued is False
    assert form.status == FormStatus.EXCEPTION_REVIEW.value
    assert form.retry_count == 3  # budget untouched — the requeue was refused


@pytest.mark.asyncio
async def test_canceled_call_never_auto_requeues(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user-ended (CANCELED) call rides the post-call pipeline for transcript
    validation, but the auto-retry edge is refused whatever the fill and flag —
    the supervisor who ended the call does not want the payer redialed."""
    tenant_id, call_id, form_id, ref = _ids()
    form = _form_row(tenant_id, form_id, completion_pct=40.0, retry_count=0)
    session = _FakeSession(
        call=_call_row(tenant_id, call_id, form_id, current_status=CallStatus.CANCELED.value),
        form=form,
        tenant=_tenant(tenant_id, max_retries=3, retry_fill_threshold=0.95),
    )
    audit = _wire(monkeypatch, session)

    requeued = await resolve_ai_processing(
        _SM, audit, ref, trigger="user_end_call", auto_retry_enabled=True
    )

    assert requeued is False
    assert form.status == FormStatus.EXCEPTION_REVIEW.value
    assert form.review_reason == ReviewReason.USER_ENDED.value  # supervisor ended it
    assert form.retry_count == 0  # budget untouched — cancels are operator decisions
    assert audit.records[0].detail["to"] == FormStatus.EXCEPTION_REVIEW.value


@pytest.mark.asyncio
async def test_end_intent_stamp_alone_blocks_auto_requeue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt-and-braces: the stamp suppresses the retry even if the resolver
    races the closeout's status write and still sees a non-CANCELED status."""
    tenant_id, call_id, form_id, ref = _ids()
    form = _form_row(tenant_id, form_id, completion_pct=40.0, retry_count=0)
    session = _FakeSession(
        call=_call_row(tenant_id, call_id, form_id, end_requested_by_id=uuid4()),
        form=form,
        tenant=_tenant(tenant_id, max_retries=3, retry_fill_threshold=0.95),
    )
    audit = _wire(monkeypatch, session)

    requeued = await resolve_ai_processing(
        _SM, audit, ref, trigger="call.ended", auto_retry_enabled=True
    )

    assert requeued is False
    assert form.status == FormStatus.EXCEPTION_REVIEW.value


@pytest.mark.asyncio
async def test_form_not_in_ai_processing_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotency: a redelivered call.ended (or a sweeper racing the consumer)
    finds the form already resolved and must not touch it again."""
    tenant_id, call_id, form_id, ref = _ids()
    form = _form_row(tenant_id, form_id, status=FormStatus.EXCEPTION_REVIEW.value)
    session = _FakeSession(
        call=_call_row(tenant_id, call_id, form_id),
        form=form,
        tenant=_tenant(tenant_id),
    )
    audit = _wire(monkeypatch, session)

    requeued = await resolve_ai_processing(_SM, audit, ref, trigger="call.ended")

    assert requeued is False
    assert form.status == FormStatus.EXCEPTION_REVIEW.value
    assert audit.records == []


@pytest.mark.asyncio
async def test_missing_call_or_form_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id, _call_id, _form_id, ref = _ids()
    session = _FakeSession(call=None, form=None, tenant=_tenant(tenant_id))
    audit = _wire(monkeypatch, session)

    assert await resolve_ai_processing(_SM, audit, ref, trigger="call.ended") is False
    assert audit.records == []


@pytest.mark.asyncio
async def test_sweep_resolves_stuck_ai_processing_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash between closeout and resolution leaves the form parked in
    AI_PROCESSING (a leaked concurrency slot) — the sweeper phase resolves it."""
    tenant_id, call_id, form_id, _ref = _ids()
    form = _form_row(tenant_id, form_id, completion_pct=100.0)
    session = _FakeSession(
        call=_call_row(tenant_id, call_id, form_id),
        form=form,
        tenant=_tenant(tenant_id),
    )
    audit = _wire(monkeypatch, session)

    resolved = await sweep_stuck_ai_processing(_SM, audit, tenant_id, grace_s=300)

    assert resolved == 1
    assert form.status == FormStatus.EXCEPTION_REVIEW.value
    assert audit.records[0].detail["trigger"] == "sweeper_ai_processing"


@pytest.mark.asyncio
async def test_sweep_with_no_stuck_forms_resolves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession(call=None, form=None, tenant=_tenant(uuid4()))
    audit = _wire(monkeypatch, session)

    assert await sweep_stuck_ai_processing(_SM, audit, uuid4(), grace_s=300) == 0
    assert audit.records == []
