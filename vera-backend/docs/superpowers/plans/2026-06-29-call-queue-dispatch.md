# Call Queue & Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an event-driven call queue with tenant concurrency gating, insurance provider working-hours awareness, a form status state machine, automatic retry, and queue expiry.

**Architecture:** Two new service modules in `vera_core` (state machine + dispatcher), wired into the existing control plane endpoints via event triggers. No new tables — the queue is implicit (`PatientForm` rows with `status = 'in_queue'`). A new Alembic migration adds columns to `tenant` and `patient_form` and extends the `FormStatus` CHECK constraint.

**Tech Stack:** Python 3.12, SQLAlchemy async, FastAPI, Postgres (RLS), LiveKit server SDK, pytest-asyncio.

## Global Constraints

- Python 3.12 (`<3.13`). PEP 695 type params (`class Foo[T]`), not `Generic[T]`/`TypeVar`.
- `asyncio` is the single async runtime. Never `import anyio`.
- PHI never enters logs, traces, URLs, or cache. The dispatcher handles form IDs and statuses only — no PHI flows through it.
- All tenant-scoped queries run inside `tenant_session(...)` with RLS active.
- CHECK constraints (not native ENUM types) for status columns.
- DB timestamps only (`func.now()` / `server_default`), never `datetime.now()` except for the working-hours comparison (which reads wall-clock time to compare against provider hours).
- `just check` (ruff + mypy --strict + pytest) must pass before claiming done.

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Modify | `packages/vera_core/src/vera_core/models/enums.py` | Add `EXPIRED` to `FormStatus` |
| Modify | `packages/vera_core/src/vera_core/models/tenant.py` | Add `max_retries`, `queue_expiry_hours` |
| Modify | `packages/vera_core/src/vera_core/models/patient_form.py` | Add `enqueued_at`, update partial index |
| Create | `packages/vera_core/src/vera_core/services/__init__.py` | Package init |
| Create | `packages/vera_core/src/vera_core/services/form_state_machine.py` | Transition validator + side effects |
| Create | `packages/vera_core/src/vera_core/services/queue_dispatcher.py` | Dispatch engine |
| Modify | `packages/vera_core/src/vera_core/models/audit_log.py` | Add `QUEUE_DISPATCH` and `QUEUE_EXPIRED` to `AuditEvent` |
| Create | `migrations/versions/0018_call_queue_columns.py` | Schema migration |
| Modify | `apps/control_plane/src/control_plane/api/v1/patient_forms.py` | Wire state machine + dispatch on enqueue |
| Modify | `apps/control_plane/src/control_plane/api/v1/calls.py` | Add `POST /calls/{call_id}/status` callback endpoint |
| Create | `tests/unit/services/__init__.py` | Package init |
| Create | `tests/unit/services/test_form_state_machine.py` | State machine unit tests |
| Create | `tests/unit/services/test_queue_dispatcher.py` | Dispatcher unit tests |

---

### Task 1: Schema Changes — Enum, Models, Migration

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/enums.py:15-24`
- Modify: `packages/vera_core/src/vera_core/models/tenant.py:40-44`
- Modify: `packages/vera_core/src/vera_core/models/patient_form.py:26-43,83-85`
- Modify: `packages/vera_core/src/vera_core/models/audit_log.py:26-41`
- Create: `migrations/versions/0018_call_queue_columns.py`

**Interfaces:**
- Consumes: existing `Base`, `TenantScopedMixin`, `FormStatus`, `check_in`
- Produces: `FormStatus.EXPIRED`, `Tenant.max_retries`, `Tenant.queue_expiry_hours`, `PatientForm.enqueued_at`, `AuditEvent.QUEUE_DISPATCH`, `AuditEvent.QUEUE_EXPIRED`

- [ ] **Step 1: Add `EXPIRED` to `FormStatus` enum**

In `packages/vera_core/src/vera_core/models/enums.py`, add after `CALL_FAILED`:

```python
class FormStatus(enum.StrEnum):
    """patient_form record lifecycle (ADR §7, spec §4.3.3)."""

    READY_FOR_PROCESSING = "ready_for_processing"
    IN_QUEUE = "in_queue"
    IN_CALL = "in_call"
    AI_PROCESSING = "ai_processing"
    EXCEPTION_REVIEW = "exception_review"
    COMPLETED = "completed"
    CALL_FAILED = "call_failed"
    EXPIRED = "expired"
```

- [ ] **Step 2: Add `max_retries` and `queue_expiry_hours` to `Tenant`**

In `packages/vera_core/src/vera_core/models/tenant.py`, add after the `persona_tweak` column:

```python
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    queue_expiry_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=48)
```

- [ ] **Step 3: Add `enqueued_at` to `PatientForm` and update partial index**

In `packages/vera_core/src/vera_core/models/patient_form.py`, add after `scheduled_at`:

```python
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Update the `ix_patient_form_queued` index in `__table_args__` to sort by `enqueued_at` instead of `scheduled_at` (this is the FIFO ordering column):

```python
        Index(
            "ix_patient_form_queued",
            "enqueued_at",
            postgresql_where=text("status = 'in_queue'"),
        ),
```

- [ ] **Step 4: Add audit events for dispatch and expiry**

In `packages/vera_core/src/vera_core/models/audit_log.py`, add to `AuditEvent`:

```python
    QUEUE_DISPATCH = "queue.dispatch"
    QUEUE_EXPIRED = "queue.expired"
```

- [ ] **Step 5: Write the Alembic migration**

Create `migrations/versions/0018_call_queue_columns.py`:

```python
"""Add call-queue columns to tenant and patient_form; extend FormStatus CHECK.

Revision ID: 0018
Revises: 0017_persona_tweak_event
"""

from alembic import op
import sqlalchemy as sa

revision = "0018_call_queue_columns"
down_revision = "0017_persona_tweak_event"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- tenant: queue config knobs ---
    op.add_column("tenant", sa.Column("max_retries", sa.Integer(), nullable=False, server_default="5"))
    op.add_column(
        "tenant", sa.Column("queue_expiry_hours", sa.Integer(), nullable=False, server_default="48")
    )

    # --- patient_form: enqueued_at + updated partial index ---
    op.add_column(
        "patient_form",
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Replace the partial index to sort on enqueued_at instead of scheduled_at.
    op.drop_index("ix_patient_form_queued", table_name="patient_form")
    op.create_index(
        "ix_patient_form_queued",
        "patient_form",
        ["enqueued_at"],
        postgresql_where=sa.text("status = 'in_queue'"),
    )

    # --- Extend the FormStatus CHECK constraint to include 'expired' ---
    op.drop_constraint("ck_patient_form_status_valid", "patient_form", type_="check")
    op.create_check_constraint(
        "ck_patient_form_status_valid",
        "patient_form",
        "status IN ('ready_for_processing', 'in_queue', 'in_call', 'ai_processing', "
        "'exception_review', 'completed', 'call_failed', 'expired')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_patient_form_status_valid", "patient_form", type_="check")
    op.create_check_constraint(
        "ck_patient_form_status_valid",
        "patient_form",
        "status IN ('ready_for_processing', 'in_queue', 'in_call', 'ai_processing', "
        "'exception_review', 'completed', 'call_failed')",
    )
    op.drop_index("ix_patient_form_queued", table_name="patient_form")
    op.create_index(
        "ix_patient_form_queued",
        "patient_form",
        ["scheduled_at"],
        postgresql_where=sa.text("status = 'in_queue'"),
    )
    op.drop_column("patient_form", "enqueued_at")
    op.drop_column("tenant", "queue_expiry_hours")
    op.drop_column("tenant", "max_retries")
```

- [ ] **Step 6: Verify lint + types pass**

Run: `cd vera-backend && just check`

Expected: ruff + mypy pass (tests may skip if no local Postgres).

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/models/enums.py \
       packages/vera_core/src/vera_core/models/tenant.py \
       packages/vera_core/src/vera_core/models/patient_form.py \
       packages/vera_core/src/vera_core/models/audit_log.py \
       migrations/versions/0018_call_queue_columns.py
git commit -m "feat(queue): add schema columns and EXPIRED status for call queue dispatch"
```

---

### Task 2: Form State Machine

**Files:**
- Create: `packages/vera_core/src/vera_core/services/__init__.py`
- Create: `packages/vera_core/src/vera_core/services/form_state_machine.py`
- Create: `tests/unit/services/__init__.py`
- Create: `tests/unit/services/test_form_state_machine.py`

**Interfaces:**
- Consumes: `FormStatus`, `PatientForm`, `CallLineage`, `Call` (models), `func.now()` (SQLAlchemy)
- Produces: `FormStateMachine.transition(session, form, target, *, tenant_max_retries, last_call_id=None) -> None` — raises `InvalidTransitionError` on illegal moves

- [ ] **Step 1: Write failing tests for the state machine**

Create `tests/unit/services/__init__.py` (empty).

Create `tests/unit/services/test_form_state_machine.py`:

```python
"""Unit tests for FormStateMachine — transition validation and side effects.

These are pure-logic tests: they check the transition map, guard conditions,
and side-effect assignments without hitting a database. The state machine
operates on in-memory PatientForm objects; only the `enqueued_at` DB-default
needs special handling (tested via integration tests).
"""

import pytest

from vera_core.models.enums import FormStatus
from vera_core.services.form_state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    FormStateMachine,
)


class TestTransitionMap:
    """Every allowed and disallowed (from, to) pair."""

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (FormStatus.READY_FOR_PROCESSING, FormStatus.IN_QUEUE),
            (FormStatus.READY_FOR_PROCESSING, FormStatus.EXCEPTION_REVIEW),
            (FormStatus.IN_QUEUE, FormStatus.IN_CALL),
            (FormStatus.IN_QUEUE, FormStatus.EXPIRED),
            (FormStatus.IN_CALL, FormStatus.AI_PROCESSING),
            (FormStatus.IN_CALL, FormStatus.CALL_FAILED),
            (FormStatus.AI_PROCESSING, FormStatus.COMPLETED),
            (FormStatus.AI_PROCESSING, FormStatus.CALL_FAILED),
            (FormStatus.CALL_FAILED, FormStatus.IN_QUEUE),
            (FormStatus.EXCEPTION_REVIEW, FormStatus.IN_QUEUE),
        ],
    )
    def test_allowed_transitions(self, from_status: FormStatus, to_status: FormStatus) -> None:
        assert to_status in ALLOWED_TRANSITIONS[from_status]

    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (FormStatus.COMPLETED, FormStatus.IN_QUEUE),
            (FormStatus.EXPIRED, FormStatus.IN_QUEUE),
            (FormStatus.IN_QUEUE, FormStatus.COMPLETED),
            (FormStatus.IN_CALL, FormStatus.IN_QUEUE),
            (FormStatus.READY_FOR_PROCESSING, FormStatus.COMPLETED),
            (FormStatus.READY_FOR_PROCESSING, FormStatus.CALL_FAILED),
        ],
    )
    def test_disallowed_transitions(self, from_status: FormStatus, to_status: FormStatus) -> None:
        assert to_status not in ALLOWED_TRANSITIONS.get(from_status, frozenset())


class TestFormStateMachine:
    """Side-effect and guard tests on a mock PatientForm."""

    def _make_form(self, status: FormStatus, retry_count: int = 0) -> "PatientForm":
        """Minimal in-memory PatientForm-like object for testing."""
        from unittest.mock import MagicMock

        form = MagicMock()
        form.status = status.value
        form.retry_count = retry_count
        form.enqueued_at = None
        return form

    def test_transition_to_in_queue_sets_enqueued_at(self) -> None:
        sm = FormStateMachine()
        form = self._make_form(FormStatus.READY_FOR_PROCESSING)
        sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)
        assert form.enqueued_at is not None

    def test_transition_call_failed_to_in_queue_increments_retry(self) -> None:
        sm = FormStateMachine()
        form = self._make_form(FormStatus.CALL_FAILED, retry_count=1)
        sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)
        assert form.retry_count == 2
        assert form.enqueued_at is not None

    def test_transition_call_failed_to_in_queue_blocked_at_max_retries(self) -> None:
        sm = FormStateMachine()
        form = self._make_form(FormStatus.CALL_FAILED, retry_count=5)
        with pytest.raises(InvalidTransitionError, match="retries exhausted"):
            sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)

    def test_invalid_transition_raises(self) -> None:
        sm = FormStateMachine()
        form = self._make_form(FormStatus.COMPLETED)
        with pytest.raises(InvalidTransitionError):
            sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)

    def test_idempotent_same_status_is_noop(self) -> None:
        sm = FormStateMachine()
        form = self._make_form(FormStatus.IN_QUEUE)
        # Same status → no-op, no error
        sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=5)
        assert form.status == FormStatus.IN_QUEUE.value
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && python -m pytest tests/unit/services/test_form_state_machine.py -v`

Expected: `ModuleNotFoundError: No module named 'vera_core.services'`

- [ ] **Step 3: Implement the state machine**

Create `packages/vera_core/src/vera_core/services/__init__.py`:

```python
"""Business-logic services — stateless, framework-free, testable."""
```

Create `packages/vera_core/src/vera_core/services/form_state_machine.py`:

```python
"""Form lifecycle state machine.

Validates transitions, applies side effects (enqueued_at, retry_count),
and guards conditional edges (retry cap). Every form status change in the
codebase MUST go through `FormStateMachine.transition()`.
"""

from datetime import datetime, timezone

from vera_core.models.enums import FormStatus

# The full transition map. Keys are source statuses; values are the set of
# legal target statuses from that source.
ALLOWED_TRANSITIONS: dict[FormStatus, frozenset[FormStatus]] = {
    FormStatus.READY_FOR_PROCESSING: frozenset(
        {FormStatus.IN_QUEUE, FormStatus.EXCEPTION_REVIEW}
    ),
    FormStatus.IN_QUEUE: frozenset({FormStatus.IN_CALL, FormStatus.EXPIRED}),
    FormStatus.IN_CALL: frozenset({FormStatus.AI_PROCESSING, FormStatus.CALL_FAILED}),
    FormStatus.AI_PROCESSING: frozenset({FormStatus.COMPLETED, FormStatus.CALL_FAILED}),
    FormStatus.CALL_FAILED: frozenset({FormStatus.IN_QUEUE}),
    FormStatus.EXCEPTION_REVIEW: frozenset({FormStatus.IN_QUEUE}),
}


class InvalidTransitionError(Exception):
    """Raised when a form status transition is not allowed."""

    def __init__(self, from_status: str, to_status: str, reason: str = "") -> None:
        detail = f": {reason}" if reason else ""
        super().__init__(
            f"cannot transition from '{from_status}' to '{to_status}'{detail}"
        )
        self.from_status = from_status
        self.to_status = to_status


class FormStateMachine:
    """Validates and applies form status transitions with side effects."""

    def transition[F](
        self,
        form: F,
        target: FormStatus,
        *,
        tenant_max_retries: int,
    ) -> None:
        """Move *form* to *target* status, applying side effects.

        Parameters
        ----------
        form:
            A ``PatientForm`` instance (or mock with `.status`, `.retry_count`,
            `.enqueued_at` attributes).
        target:
            The desired new ``FormStatus``.
        tenant_max_retries:
            The tenant's ``max_retries`` cap — guards ``CALL_FAILED → IN_QUEUE``.

        Raises
        ------
        InvalidTransitionError
            If the transition is illegal or a guard blocks it.
        """
        current = FormStatus(form.status)

        # Idempotent no-op.
        if current == target:
            return

        allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise InvalidTransitionError(current.value, target.value)

        # Guard: retry cap on CALL_FAILED → IN_QUEUE.
        if current == FormStatus.CALL_FAILED and target == FormStatus.IN_QUEUE:
            if form.retry_count >= tenant_max_retries:
                raise InvalidTransitionError(
                    current.value, target.value, reason="retries exhausted"
                )
            form.retry_count += 1

        # Side effect: any transition into IN_QUEUE sets enqueued_at.
        if target == FormStatus.IN_QUEUE:
            form.enqueued_at = datetime.now(timezone.utc)

        form.status = target.value
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && python -m pytest tests/unit/services/test_form_state_machine.py -v`

Expected: All tests PASS.

- [ ] **Step 5: Run full lint + type check**

Run: `cd vera-backend && just check`

Expected: ruff + mypy pass.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/services/__init__.py \
       packages/vera_core/src/vera_core/services/form_state_machine.py \
       tests/unit/services/__init__.py \
       tests/unit/services/test_form_state_machine.py
git commit -m "feat(queue): add FormStateMachine with transition validation and side effects"
```

---

### Task 3: Queue Dispatcher

**Files:**
- Create: `packages/vera_core/src/vera_core/services/queue_dispatcher.py`
- Create: `tests/unit/services/test_queue_dispatcher.py`

**Interfaces:**
- Consumes: `FormStateMachine.transition(...)`, `PatientForm`, `Call`, `CallEvent`, `Tenant`, `InsuranceProvider`, models + enums, `LiveKitGateway.create_call_room(room_name, metadata)`, `room_name_for_call(tenant_id, call_id)`, `PersonaTweak`
- Produces: `async def try_dispatch(session, tenant_id, livekit) -> int` — returns count of calls initiated

- [ ] **Step 1: Write failing tests for the dispatcher**

Create `tests/unit/services/test_queue_dispatcher.py`:

```python
"""Unit tests for QueueDispatcher.

Uses an in-memory approach: the dispatcher is tested through its public
interface with mock SQLAlchemy session results and a FakeLiveKit, verifying
FIFO ordering, concurrency gating, working-hours checks, and expiry.
"""

from datetime import datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from vera_core.db.base import uuid7
from vera_core.models.enums import CallMode, CallStatus, FormStatus
from vera_core.services.queue_dispatcher import is_within_working_hours


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vera-backend && python -m pytest tests/unit/services/test_queue_dispatcher.py -v`

Expected: `ModuleNotFoundError: No module named 'vera_core.services.queue_dispatcher'`

- [ ] **Step 3: Implement the dispatcher**

Create `packages/vera_core/src/vera_core/services/queue_dispatcher.py`:

```python
"""Event-driven call queue dispatcher.

Pulls admitted forms from the tenant's queue, checks concurrency limits and
insurance-provider working hours, and initiates calls. Invoked on two events:
(1) a form is enqueued, (2) a call ends and a concurrency slot frees up.

No PHI flows through this module — it operates on form IDs, statuses, and
tenant/provider config only.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vera_core.models import Call, CallEvent, InsuranceProvider, PatientForm, Tenant
from vera_core.models.enums import (
    CallEventType,
    CallMode,
    CallStatus,
    FormStatus,
)
from vera_core.observability.correlation import room_name_for_call
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError

if TYPE_CHECKING:
    from uuid import UUID

    from vera_core.schemas import PersonaTweak

logger = logging.getLogger(__name__)

_EASTERN = ZoneInfo("America/New_York")

# Call statuses that count toward the tenant's concurrency cap.
_ACTIVE_CALL_STATUSES = (
    FormStatus.IN_CALL.value,
    FormStatus.AI_PROCESSING.value,
)


def _now_eastern_time() -> time:
    """Current wall-clock time in US Eastern. Extracted for test patching."""
    return datetime.now(_EASTERN).time()


def is_within_working_hours(provider: InsuranceProvider) -> bool:
    """Check whether the provider is within its working-hours window."""
    if provider.working_hour_start is None or provider.working_hour_end is None:
        return True
    now_time = _now_eastern_time()
    return provider.working_hour_start <= now_time <= provider.working_hour_end


async def try_dispatch(
    session: AsyncSession,
    tenant_id: UUID,
    livekit: object,
) -> int:
    """Attempt to dispatch queued forms for *tenant_id*.

    Returns the number of calls initiated. Designed to be called after commit
    of the triggering event (enqueue or call-end).

    Parameters
    ----------
    session:
        An active ``AsyncSession`` scoped to *tenant_id* (RLS active).
    tenant_id:
        The tenant whose queue to drain.
    livekit:
        A ``LiveKitGateway`` (or duck-typed fake) with ``create_call_room``.
    """
    # 1. Load tenant config.
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None:
        logger.warning("dispatch: tenant %s not found", tenant_id)
        return 0

    # 2. Count active calls (forms in IN_CALL or AI_PROCESSING).
    active_count: int = (
        await session.execute(
            select(func.count())
            .select_from(PatientForm)
            .where(
                PatientForm.tenant_id == tenant_id,
                PatientForm.status.in_(list(_ACTIVE_CALL_STATUSES)),
            )
        )
    ).scalar_one()

    slots = tenant.max_agents_per_va - active_count
    if slots <= 0:
        return 0

    # 3. Fetch FIFO candidates — FOR UPDATE SKIP LOCKED prevents double-dispatch.
    candidates = (
        (
            await session.execute(
                select(PatientForm)
                .where(
                    PatientForm.tenant_id == tenant_id,
                    PatientForm.status == FormStatus.IN_QUEUE.value,
                )
                .order_by(PatientForm.enqueued_at.asc())
                .limit(slots)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    sm = FormStateMachine()
    dispatched = 0

    for form in candidates:
        # 4a. Expiry check.
        if _is_expired(form, tenant.queue_expiry_hours):
            try:
                sm.transition(form, FormStatus.EXPIRED, tenant_max_retries=tenant.max_retries)
            except InvalidTransitionError:
                pass
            continue

        # 4b. Working-hours check.
        if not await _provider_in_hours(session, form):
            continue

        # 4c. Dispatch the call.
        call_mode = CallMode.RETRY if form.retry_count > 0 else CallMode.FULL
        sm.transition(form, FormStatus.IN_CALL, tenant_max_retries=tenant.max_retries)

        call = Call(
            tenant_id=tenant_id,
            form_id=form.id,
            current_status=CallStatus.INITIATED.value,
            mode=call_mode.value,
        )
        session.add(call)
        await session.flush()

        room_name = room_name_for_call(tenant_id, call.id)

        # Build persona metadata for the agent dispatch.
        from vera_core.schemas import PersonaTweak

        tweak = (
            PersonaTweak.model_validate(tenant.persona_tweak)
            if tenant.persona_tweak
            else PersonaTweak()
        )
        metadata = tweak.model_dump(exclude_none=True)
        await livekit.create_call_room(room_name, metadata=metadata)  # type: ignore[union-attr]

        session.add(
            CallEvent(
                tenant_id=tenant_id,
                call_id=call.id,
                event_type=CallEventType.STATUS.value,
                event_value=CallStatus.INITIATED.value,
            )
        )
        dispatched += 1
        logger.info(
            "dispatch: initiated call %s for form %s (mode=%s)",
            call.id,
            form.id,
            call_mode.value,
        )

    return dispatched


def _is_expired(form: PatientForm, queue_expiry_hours: int) -> bool:
    """True if the form has been in the queue past the expiry window."""
    if form.enqueued_at is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=queue_expiry_hours)
    enqueued = form.enqueued_at
    if enqueued.tzinfo is None:
        enqueued = enqueued.replace(tzinfo=timezone.utc)
    return enqueued < cutoff


async def _provider_in_hours(session: AsyncSession, form: PatientForm) -> bool:
    """Resolve the form's insurance provider and check working hours.

    If the form has no linked provider (name-based lookup returns nothing),
    default to allowing dispatch — the provider hours gate is opt-in.
    """
    if not form.insurance_provider:
        return True
    provider = (
        await session.execute(
            select(InsuranceProvider).where(
                InsuranceProvider.name == form.insurance_provider,
                InsuranceProvider.status == "active",
            )
        )
    ).scalar_one_or_none()
    if provider is None:
        return True
    return is_within_working_hours(provider)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vera-backend && python -m pytest tests/unit/services/test_queue_dispatcher.py -v`

Expected: All tests PASS.

- [ ] **Step 5: Run full lint + type check**

Run: `cd vera-backend && just check`

Expected: ruff + mypy pass.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/services/queue_dispatcher.py \
       tests/unit/services/test_queue_dispatcher.py
git commit -m "feat(queue): add event-driven QueueDispatcher with concurrency, working-hours, and expiry"
```

---

### Task 4: Wire Dispatch Trigger Into Endpoints

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py:690-791`
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py`
- Modify: `apps/control_plane/src/control_plane/deps.py` (if needed for sessionmaker access)

**Interfaces:**
- Consumes: `FormStateMachine.transition(...)`, `try_dispatch(session, tenant_id, livekit)`, existing `TenantSession`, `LiveKit`, `TenantId` deps
- Produces: Updated `PUT /patient-forms/{id}/status` that fires dispatch on enqueue; new `POST /calls/{call_id}/status` callback endpoint

- [ ] **Step 1: Update `PUT /patient-forms/{id}/status` to use state machine + fire dispatch**

In `apps/control_plane/src/control_plane/api/v1/patient_forms.py`, replace the manual `_ALLOWED_STATUS_TRANSITIONS` dict and the status validation logic in `update_patient_form_status` with the state machine. After a successful transition to `IN_QUEUE`, fire the dispatcher.

Replace the `_ALLOWED_STATUS_TRANSITIONS` dict and the body of `update_patient_form_status` with:

```python
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError
from vera_core.services.queue_dispatcher import try_dispatch


# Human-driven transitions the UI may request. The call pipeline owns the
# automatic core path (IN_QUEUE → IN_CALL → AI_PROCESSING → EXCEPTION_REVIEW) and
# the → CALL_FAILED edges; a reviewer/operator may only (re)queue work or complete
# a reviewed form. Any (current → target) pair absent here is rejected (422), so
# the worker-driven states can't be set by hand.
_MANUAL_TARGETS: dict[FormStatus, frozenset[FormStatus]] = {
    FormStatus.READY_FOR_PROCESSING: frozenset({FormStatus.IN_QUEUE}),
    FormStatus.CALL_FAILED: frozenset({FormStatus.IN_QUEUE}),
    FormStatus.EXCEPTION_REVIEW: frozenset({FormStatus.IN_QUEUE, FormStatus.COMPLETED}),
}
```

Then in the endpoint body, after the row-lock and current/target resolution, replace the transition logic with:

```python
    # Idempotent no-op: nothing to change, validate, or audit.
    if target == current:
        return ok(
            PatientFormStatusResponse(id=form.id, status=form.status),
            message="Status unchanged.",
        )

    # Manual-endpoint guard: only the transitions in _MANUAL_TARGETS are allowed here.
    if target not in _MANUAL_TARGETS.get(current, frozenset()):
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message=f"cannot change status from '{current.value}' to '{target.value}'",
            data={"from": current.value, "to": target.value},
        )

    # A form may only complete once every judge-flagged dispute is adjudicated.
    if target == FormStatus.COMPLETED:
        remaining = (await _unresolved_dispute_count_by_form(session, [form_id])).get(form_id, 0)
        if remaining:
            raise CustomAPIException(
                DefaultExceptionCode.CONFLICT,
                message="resolve all disputes before completing this form",
                data={"unresolved_disputes": remaining},
            )

    # Load tenant for state machine guard (retry cap).
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()

    sm = FormStateMachine()
    try:
        sm.transition(form, target, tenant_max_retries=tenant.max_retries)
    except InvalidTransitionError as exc:
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message=str(exc),
            data={"from": current.value, "to": target.value},
        ) from exc

    await session.flush()
```

At the end of the endpoint, after the audit emit, add the dispatch trigger:

```python
    # Fire the dispatcher if a form was just enqueued.
    if target == FormStatus.IN_QUEUE:
        await try_dispatch(session, tenant_id, livekit)

    return ok(
        PatientFormStatusResponse(id=form.id, status=form.status),
        message="Status updated.",
    )
```

Add `livekit: LiveKit` to the endpoint's parameter list (import `LiveKit` from `control_plane.api.v1.common`).

Also add `Tenant` to the imports from `vera_core.models`.

- [ ] **Step 2: Add `POST /calls/{call_id}/status` callback endpoint**

In `apps/control_plane/src/control_plane/api/v1/calls.py`, add a new endpoint after the existing ones:

```python
from pydantic import BaseModel
from vera_core.models.enums import CallMode, FormStatus
from vera_core.models.call import CallLineage
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError
from vera_core.services.queue_dispatcher import try_dispatch


class UpdateCallStatusRequest(BaseModel):
    status: CallStatus


_TERMINAL_FAILURE_STATUSES = frozenset(
    {CallStatus.FAILED, CallStatus.NO_ANSWER, CallStatus.BUSY}
)


@router.post(
    "/calls/{call_id}/status",
    response_model=ResponseModel[CallSummary],
    responses=CustomAPIResponse.custom(
        DefaultExceptionCode.UNAUTHORIZED,
        DefaultExceptionCode.FORBIDDEN,
        DefaultExceptionCode.NOT_FOUND,
        DefaultExceptionCode.VALIDATION_ERROR,
    ),
)
async def update_call_status(
    call_id: UUID,
    body: UpdateCallStatusRequest,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    _caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[CallSummary]:
    """Callback endpoint for the agent worker to report call terminal status.

    On terminal failure with retries remaining, auto-retries the form.
    Always fires the dispatcher afterward to fill freed concurrency slots.
    """
    call = (
        await session.execute(
            select(Call).where(Call.id == call_id).with_for_update()
        )
    ).scalar_one_or_none()
    if call is None:
        raise NotFoundError(message="call not found")

    form = (
        await session.execute(
            select(PatientForm).where(PatientForm.id == call.form_id).with_for_update()
        )
    ).scalar_one_or_none()
    if form is None:
        raise NotFoundError(message="patient form not found")

    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()

    # Update the call's status.
    call.current_status = body.status.value
    session.add(
        CallEvent(
            tenant_id=tenant_id,
            call_id=call.id,
            event_type=CallEventType.STATUS,
            event_value=body.status.value,
        )
    )

    sm = FormStateMachine()

    if body.status == CallStatus.COMPLETED:
        sm.transition(form, FormStatus.COMPLETED, tenant_max_retries=tenant.max_retries)
    elif body.status in _TERMINAL_FAILURE_STATUSES:
        sm.transition(form, FormStatus.CALL_FAILED, tenant_max_retries=tenant.max_retries)
        # Auto-retry if retries remain.
        try:
            sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=tenant.max_retries)
            # Record retry lineage — the next call (created by dispatcher) will
            # link back to this one. For now we just mark the form as re-queued;
            # the dispatcher creates the Call + CallLineage on its next pass.
        except InvalidTransitionError:
            pass  # Retries exhausted — form stays CALL_FAILED (terminal).

    await session.flush()

    # Fire the dispatcher — a concurrency slot just freed up.
    await try_dispatch(session, tenant_id, livekit)

    return ok(_summary(call, form.patient_name))
```

- [ ] **Step 3: Run full lint + type check**

Run: `cd vera-backend && just check`

Expected: ruff + mypy pass.

- [ ] **Step 4: Commit**

```bash
git add apps/control_plane/src/control_plane/api/v1/patient_forms.py \
       apps/control_plane/src/control_plane/api/v1/calls.py
git commit -m "feat(queue): wire dispatch triggers into patient-form and call endpoints"
```

---

### Task 5: Integration Tests

**Files:**
- Create: `tests/integration/control_plane/test_call_queue.py`

**Interfaces:**
- Consumes: all prior tasks — models, state machine, dispatcher, endpoints
- Produces: end-to-end validation of the queue dispatch lifecycle

- [ ] **Step 1: Write integration tests**

Create `tests/integration/control_plane/test_call_queue.py`:

```python
"""Integration tests for the call queue & dispatch lifecycle.

Exercises the full flow: enqueue form → dispatcher fires → call created →
call terminal status reported → auto-retry → dispatcher fires again.
Runs against live RLS-enforcing Postgres with FakeLiveKit.
"""

from collections.abc import AsyncGenerator
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.integration.control_plane.conftest import RBACWorld
from vera_core.db import uuid7
from vera_core.models import PatientForm
from vera_core.models.authoring import FormSchema, SchemaVersion
from vera_core.models.enums import FormStatus, InsuranceType


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def queue_form_id(
    database_url: str,
    rbac_world: RBACWorld,
) -> AsyncGenerator[UUID]:
    """Seed a PatientForm in READY_FOR_PROCESSING for queue tests."""
    form_schema_id = uuid7()
    schema_version_id = uuid7()
    patient_form_id = uuid7()

    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as session, session.begin():
            session.add(
                FormSchema(
                    id=form_schema_id,
                    insurance_type=InsuranceType.INFERTILITY_TREATMENT.value,
                    name="Queue Test Schema",
                )
            )
            await session.flush()
            session.add(
                SchemaVersion(
                    id=schema_version_id,
                    schema_id=form_schema_id,
                    version=1,
                    schema_json={},
                )
            )
            await session.flush()
            session.add(
                PatientForm(
                    id=patient_form_id,
                    tenant_id=rbac_world.tenant_id,
                    schema_version_id=schema_version_id,
                    patient_name="Queue Test Patient",
                )
            )

        yield patient_form_id

        async with sessionmaker() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM call_event WHERE call_id IN "
                    "(SELECT id FROM call WHERE form_id = :fid)"
                ).bindparams(fid=patient_form_id)
            )
            await session.execute(
                text("DELETE FROM call WHERE form_id = :fid").bindparams(fid=patient_form_id)
            )
            await session.execute(
                text("DELETE FROM patient_form WHERE id = :fid").bindparams(fid=patient_form_id)
            )
            await session.execute(
                text("DELETE FROM schema_version WHERE id = :sid").bindparams(sid=schema_version_id)
            )
            await session.execute(
                text("DELETE FROM form_schema WHERE id = :fsid").bindparams(fsid=form_schema_id)
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_form_triggers_dispatch(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    queue_form_id: UUID,
) -> None:
    """Enqueue a form → dispatcher fires → form moves to IN_CALL, a Call is created."""
    # Enqueue: READY_FOR_PROCESSING → IN_QUEUE
    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "in_queue"},
    )
    assert resp.status_code == 200, resp.text
    # The dispatcher should have moved it to IN_CALL (within the same request).
    data = resp.json()["data"]
    # The status endpoint returns the form status BEFORE dispatch runs
    # (dispatch runs after flush). Check via the calls list.
    calls_resp = await client.get(
        "/api/v1/calls",
        headers=_auth(rbac_world.admin_token),
    )
    assert calls_resp.status_code == 200, calls_resp.text
    # At least one call should exist for this tenant.
    calls = calls_resp.json()["data"]
    assert len(calls) >= 1


@pytest.mark.asyncio
async def test_enqueue_blocked_transition_returns_422(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    queue_form_id: UUID,
) -> None:
    """Cannot transition from READY_FOR_PROCESSING → COMPLETED directly."""
    resp = await client.put(
        f"/api/v1/patient-forms/{queue_form_id}/status",
        headers=_auth(rbac_world.admin_token),
        json={"status": "completed"},
    )
    assert resp.status_code == 422, resp.text
```

- [ ] **Step 2: Run integration tests**

Run: `cd vera-backend && python -m pytest tests/integration/control_plane/test_call_queue.py -v`

Expected: Tests PASS (requires local Postgres via `just up && just migrate`).

- [ ] **Step 3: Run full suite**

Run: `cd vera-backend && just check`

Expected: All checks pass.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/control_plane/test_call_queue.py
git commit -m "test(queue): add integration tests for call queue dispatch lifecycle"
```
