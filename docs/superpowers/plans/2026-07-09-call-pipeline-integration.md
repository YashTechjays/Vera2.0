# Call Pipeline Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the loop on the real call flow: form → In_Queue (with a queueability gate) → dispatcher dials the payer via SIP (1 s pacing) → worker reports answered/ended/failed over the Redis worker-event bus → consumer updates Call + PatientForm rows and refills freed slots → Live Monitoring shows the call with a live transcript via a new all-event-types SSE.

**Architecture:** The queue dispatcher becomes the *only* production call initiator (the manual `POST /calls` endpoint and the user-session-guarded `POST /calls/{id}/status` callback are removed). The DB-less agent worker signals lifecycle over the existing Redis Streams worker-event bus (`call.answered` / `call.ended` join `call.failed`); the control-plane consumer becomes the single writer of worker-driven Call/Form state, via a shared `close_call` terminal path. A **pipeline sweeper** background loop closes the two timing holes: it reconciles stuck calls (non-terminal Call + LiveKit room gone ⇒ worker died / event lost; hard duration cap for wedged sessions) through the same terminal path, and wakes the dispatcher on a timer (working-hours reopen, queue expiry, leaked slots). A new generalized per-call Redis event stream (envelope `{type, data, ts}` — transcript today, form-fill later) feeds a new `GET /calls/{call_id}/events` SSE that the Live Monitoring modal renders. Voice Lab stays untouched as the sandbox.

**Tech Stack:** FastAPI + SQLAlchemy async + Redis Streams (backend), livekit-agents worker, React/Vite/TS frontend.

## Global Constraints

- **PHI:** the call-event stream carries only tokenized/de-identified text (same contract as `vera_core/transcript.py`); room/dispatch metadata never carries PHI; audit field **names**, never values; timestamps via `func.now()` (DB clock) for rows.
- **State machine:** every form status change goes through `FormStateMachine.transition()` (`vera_core/services/form_state_machine.py`).
- **Layering:** `vera_core` must not import from `control_plane` or `agent_worker`.
- **Redis Streams:** blocking reads RAISE `redis.exceptions.TimeoutError` on idle — handle as an idle tick (see `vera-backend/CLAUDE.md`).
- **asyncio only** — never import anyio. PEP 695 generics only.
- Backend gate: `just check` (ruff + mypy --strict + pytest) from `vera-backend/`. Frontend gate: `npm run build && npm run lint && npm test` from `vera-frontend/`.
- Migrations: exactly one — `patient_form.ivr_navigation_enabled` (Task 2). It MUST follow the repo's idempotent rules (`ADD COLUMN IF NOT EXISTS`; random-hex revision id from `just makemigration`, never hand-numbered) because migration `0001` create_all gives fresh DBs the column already.
- Commits: conventional messages, **no Co-Authored-By lines**.
- After all tasks: run the **code-simplifier** agent on the change, re-run gates (repo CLAUDE.md mandate), and **boot-verify** the consumer loop (backend CLAUDE.md: background-loop changes must be verified by booting, not pytest alone).
- Out of scope (explicit user decisions): working-hours/40-min queue gate (skipped), post-queue UI navigation (skipped), form context to the worker / AI_CALL answer write-back (another developer), hiding the IVR-navigation toggle behind a feature flag (later — the toggle is a test-phase opt-out; production default is navigation ON).

**Paths:** backend = `vera-backend/`, frontend = `vera-frontend/` (both under the repo root). All backend paths below are relative to `vera-backend/`, frontend paths to `vera-frontend/`.

---

### Task 1: Move telephony errors + IVR playbook selection into vera_core (layering prep)

The dispatcher (vera_core) must catch `OutboundDialError` and call `add_active_playbook_metadata`, both currently defined in `control_plane`. Move them down; re-export for existing callers.

**Files:**
- Create: `packages/vera_core/src/vera_core/telephony.py`
- Create: `packages/vera_core/src/vera_core/services/ivr_selection.py`
- Modify: `apps/control_plane/src/control_plane/livekit_gateway.py` (delete local exception classes, import from vera_core)
- Modify: `apps/control_plane/src/control_plane/ivr_selection.py` (delete — move body to vera_core)
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py:30`, `apps/control_plane/src/control_plane/api/v1/voice_lab.py:37-38` (imports)
- Test: existing suites cover both (`tests/integration/control_plane/test_voice_lab.py`, `test_ivr_playbooks.py`) — no new tests, this is a pure move.

**Interfaces:**
- Produces: `vera_core.telephony.OutboundDialError`, `vera_core.telephony.LiveKitUnavailable`; `vera_core.services.ivr_selection.add_active_playbook_metadata(session, provider_id: UUID | None, metadata: dict[str, Any]) -> None` (unchanged signature).

- [ ] **Step 1: Create `packages/vera_core/src/vera_core/telephony.py`**

```python
"""Telephony-seam error types, shared by the control-plane LiveKit gateway (raiser)
and the vera_core queue dispatcher (catcher). vera_core must not import control_plane,
so the exception types live here."""


class LiveKitUnavailable(Exception):
    """The LiveKit SIP service could not be reached (or errored) while we probed it —
    e.g. verifying a trunk id exists before storing the credential. Distinct from
    "trunk not found": this means we could not get an answer, so we fail closed."""


class OutboundDialError(Exception):
    """Placing an outbound SIP call failed at the LiveKit / telephony seam — a
    bad/deleted trunk, the provider rejecting the call, or LiveKit being unreachable."""
```

- [ ] **Step 2: Update `livekit_gateway.py`** — delete its `LiveKitUnavailable` and `OutboundDialError` class definitions (keep their docstrings' intent, now in vera_core) and add at the top:

```python
from vera_core.telephony import LiveKitUnavailable, OutboundDialError
```

Keep the names importable from `control_plane.livekit_gateway` (this import at module top level already re-exports them for `voice_lab.py`'s `from control_plane.livekit_gateway import OutboundDialError`). Add both names to `__all__` if the module has one; otherwise leave as plain re-export.

- [ ] **Step 3: Move `add_active_playbook_metadata`** — create `packages/vera_core/src/vera_core/services/ivr_selection.py` containing the entire current body of `apps/control_plane/src/control_plane/ivr_selection.py` (module docstring included, imports unchanged — they are all vera_core imports already). Delete `apps/control_plane/src/control_plane/ivr_selection.py`.

- [ ] **Step 4: Fix imports at the two call sites**

In `apps/control_plane/src/control_plane/api/v1/calls.py` and `apps/control_plane/src/control_plane/api/v1/voice_lab.py` replace:

```python
from control_plane.ivr_selection import add_active_playbook_metadata
```

with:

```python
from vera_core.services.ivr_selection import add_active_playbook_metadata
```

- [ ] **Step 5: Run the gate**

Run: `just check` (from `vera-backend/`)
Expected: PASS (pure move; existing voice-lab/IVR tests still green)

- [ ] **Step 6: Commit**

```bash
git add -A vera-backend
git commit -m "refactor(core): move telephony errors and IVR playbook selection into vera_core"
```

---

### Task 2: Status endpoint — queueability gate + IVR-navigation toggle

Two changes to the In_Queue request: (a) reject it when the form could never be dialed — missing/invalid E.164 payer phone, or no outbound SIP trunk configured (working-hours check deliberately skipped — user decision); (b) accept a voice-lab-style `enable_ivr_navigation` toggle and **persist it on the form** — dispatch may run long after this request (freed slot, sweeper tick, auto-retry), so the choice must live on the row, and retries then keep it.

**The flag defaults to TRUE** (model default + column `DEFAULT true`): every real insurance-provider call must navigate the payer IVR, so navigation-on is the natural state and the toggle is a test-phase escape hatch for calls that don't target a real IVR. (Hiding the toggle entirely behind a feature flag is a later follow-up — explicitly out of scope.)

**Files:**
- Create: `apps/control_plane/src/control_plane/queueability.py`
- Modify: `packages/vera_core/src/vera_core/models/patient_form.py` (add `ivr_navigation_enabled` column)
- Create: migration via `just makemigration` (idempotent `ADD COLUMN IF NOT EXISTS`)
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py` (wire gate + toggle into `update_patient_form_status`)
- Modify: `apps/control_plane/src/control_plane/api/v1/voice_lab.py:61` (reuse the shared E.164 regex)
- Test: `tests/unit/control_plane/test_queueability.py` (new), `tests/integration/control_plane/test_call_queue.py` (existing enqueue tests need a trunk credential + phone in their fixtures; new toggle tests)

**Interfaces:**
- Consumes: `get_integration_credentials(session, kms, *, integration_type_name)` from `vera_core.integrations.credentials`.
- Produces: `ensure_queueable(session: AsyncSession, kms: KeyManagementService, form: PatientForm) -> None` (raises `CustomAPIException`), `E164_RE: re.Pattern[str]` — both in `control_plane.queueability`; `PatientForm.ivr_navigation_enabled: bool` (NOT NULL, **default True** — real payer calls always navigate the IVR; the toggle is a test-phase opt-out) — read by the dispatcher in Task 4; `UpdateStatusRequest.enable_ivr_navigation: bool | None` (None = keep the stored value); `PatientFormDetail.ivr_navigation_enabled: bool` in the GET detail response — the UI pre-loads the toggle from it (Task 12).

- [ ] **Step 1: Write the failing tests** — `tests/unit/control_plane/test_queueability.py`

```python
"""ensure_queueable — the enqueue-time gate: a form must be dialable before it may queue."""

from types import SimpleNamespace

import pytest

from control_plane.exceptions import CustomAPIException
from control_plane.queueability import ensure_queueable


class _FakeSession:  # ensure_queueable only passes the session through to the creds lookup
    pass


def _form(phone: str | None) -> SimpleNamespace:
    return SimpleNamespace(insurance_provider_phone_number=phone)


async def _creds_present(session, kms, *, integration_type_name):
    assert integration_type_name == "livekit_outbound_trunk_id"
    return {"trunk_id": "ST_trunk"}


async def _creds_missing(session, kms, *, integration_type_name):
    return None


@pytest.mark.asyncio
async def test_rejects_missing_phone(monkeypatch):
    monkeypatch.setattr(
        "control_plane.queueability.get_integration_credentials", _creds_present
    )
    with pytest.raises(CustomAPIException) as exc:
        await ensure_queueable(_FakeSession(), object(), _form(None))
    assert "phone" in str(exc.value.message).lower()


@pytest.mark.asyncio
async def test_rejects_non_e164_phone(monkeypatch):
    monkeypatch.setattr(
        "control_plane.queueability.get_integration_credentials", _creds_present
    )
    with pytest.raises(CustomAPIException):
        await ensure_queueable(_FakeSession(), object(), _form("555-1234"))


@pytest.mark.asyncio
async def test_rejects_when_trunk_not_configured(monkeypatch):
    monkeypatch.setattr(
        "control_plane.queueability.get_integration_credentials", _creds_missing
    )
    with pytest.raises(CustomAPIException) as exc:
        await ensure_queueable(_FakeSession(), object(), _form("+15551234567"))
    assert "trunk" in str(exc.value.message).lower()


@pytest.mark.asyncio
async def test_accepts_dialable_form(monkeypatch):
    monkeypatch.setattr(
        "control_plane.queueability.get_integration_credentials", _creds_present
    )
    await ensure_queueable(_FakeSession(), object(), _form("+15551234567"))  # no raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test tests/unit/control_plane/test_queueability.py -v`
Expected: FAIL — `ModuleNotFoundError: control_plane.queueability`

- [ ] **Step 3: Create `apps/control_plane/src/control_plane/queueability.py`**

```python
"""Enqueue-time dialability gate. `PUT /patient-forms/{id}/status` → IN_QUEUE calls
this BEFORE the state-machine transition so a form that could never be dialed is
rejected with an actionable error instead of sitting in queue until expiry.

Deliberately narrow: only hard blockers (no payer phone, no outbound trunk). Soft
conditions the dispatcher already handles at dial time (working hours, concurrency)
are NOT re-checked here.
"""

import re
from typing import TYPE_CHECKING

from control_plane.exceptions import CustomAPIException, DefaultExceptionCode
from vera_core.integrations.credentials import get_integration_credentials

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from vera_core.config.kms import KeyManagementService
    from vera_core.models import PatientForm

# E.164: a leading + and 1-15 digits, first digit non-zero. Shared with voice_lab.
E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

TRUNK_INTEGRATION = "livekit_outbound_trunk_id"


async def ensure_queueable(
    session: "AsyncSession", kms: "KeyManagementService", form: "PatientForm"
) -> None:
    """Raise if *form* cannot possibly be dialed once dispatched."""
    phone = form.insurance_provider_phone_number
    if not phone or not E164_RE.match(phone):
        raise CustomAPIException(
            DefaultExceptionCode.VALIDATION_ERROR,
            message="form has no valid insurance provider phone number (E.164 required)",
            data={"field": "insurance_provider_phone_number"},
        )
    creds = await get_integration_credentials(
        session, kms, integration_type_name=TRUNK_INTEGRATION
    )
    if not (creds or {}).get("trunk_id"):
        raise CustomAPIException(
            DefaultExceptionCode.CONFLICT,
            message="outbound calling is not configured for this tenant (missing SIP trunk)",
        )
```

- [ ] **Step 4: Run unit tests to verify they pass**

Run: `just test tests/unit/control_plane/test_queueability.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Wire the gate into the status endpoint**

In `apps/control_plane/src/control_plane/api/v1/patient_forms.py`:

Add imports:

```python
from control_plane.api.v1.common import Kms, LiveKit, TenantId, TenantSession
from control_plane.queueability import ensure_queueable
```

Add `kms: Kms,` to `update_patient_form_status`'s parameters (after `livekit: LiveKit,`). Then, immediately after the `_MANUAL_TARGETS` guard block (after the `raise CustomAPIException(... "cannot change status from ...")` block, before the `→ COMPLETED` dispute check), insert:

```python
    # Hard dialability gate: a form that can never be dialed must not enter the queue.
    if target == FormStatus.IN_QUEUE:
        await ensure_queueable(session, kms, form)
```

- [ ] **Step 6: Add the `ivr_navigation_enabled` column + migration**

In `packages/vera_core/src/vera_core/models/patient_form.py`, next to `retry_count`/`enqueued_by_id` (import `Boolean` from sqlalchemy if not present):

```python
    # Operator's per-form choice at queue time (voice-lab-style toggle): should the
    # dispatched worker boot the IVR navigator for this call? Defaults TRUE — every
    # real payer call must navigate the IVR; turning it off is a test-phase escape
    # hatch (to be hidden behind a feature flag later). Persisted on the row because
    # dispatch runs later (freed slot / sweeper / auto-retry) and retries keep the
    # choice. Non-PHI config.
    ivr_navigation_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
```

(`text` is imported from sqlalchemy.) Then `just makemigration` and edit the generated file to the repo's idempotent form (migration `0001` create_all already gives fresh DBs the column — see `vera-backend/CLAUDE.md`):

```python
def upgrade() -> None:
    op.execute(
        "ALTER TABLE patient_form ADD COLUMN IF NOT EXISTS "
        "ivr_navigation_enabled boolean NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE patient_form DROP COLUMN IF EXISTS ivr_navigation_enabled")
```

Keep the autogenerated random-hex revision id (never hand-number). Run `just migrate` against the local DB.

- [ ] **Step 7: Accept and persist the toggle in the status endpoint**

In `apps/control_plane/src/control_plane/api/v1/patient_forms.py`, extend the request model:

```python
class UpdateStatusRequest(BaseModel):
    status: FormStatus  # validated against the lifecycle enum (unknown value → 422)
    # Voice-lab-style toggle, meaningful only on → IN_QUEUE: should the dispatched
    # call run the IVR navigator? None keeps the form's stored choice (so a requeue
    # without the field preserves the operator's earlier decision).
    enable_ivr_navigation: bool | None = None
```

and inside `update_patient_form_status`, in the existing `if target == FormStatus.IN_QUEUE:` block that sets `enqueued_at`/`enqueued_by_id`, add:

```python
        if body.enable_ivr_navigation is not None:
            form.ivr_navigation_enabled = body.enable_ivr_navigation
```

Expose the stored flag on the review detail so the UI's toggle reflects it on requeue — in the `PatientFormDetail` response model add `ivr_navigation_enabled: bool`, and in `_build_detail` populate `ivr_navigation_enabled=form.ivr_navigation_enabled`. (Operational dial config, not PHI — no change to the PHI-access audit's field list.)

Also include the choice in the status-change audit detail (evidence of the operator's dial configuration): change the audit `detail` dict to:

```python
            detail={
                "from": current.value,
                "to": target.value,
                **(
                    {"ivr_navigation": form.ivr_navigation_enabled}
                    if target == FormStatus.IN_QUEUE
                    else {}
                ),
            },
```

- [ ] **Step 8: Toggle integration tests** — in `tests/integration/control_plane/test_call_queue.py`:

```python
async def test_intake_defaults_ivr_navigation_on(...):
    # POST /patient-forms (intake) → row has ivr_navigation_enabled=True (column default)

async def test_enqueue_can_disable_ivr_navigation(...):
    # PUT status {"status": "in_queue", "enable_ivr_navigation": false}
    # → form.ivr_navigation_enabled is False in the DB (test-phase opt-out)

async def test_requeue_without_toggle_keeps_stored_choice(...):
    # form with ivr_navigation_enabled=False in CALL_FAILED
    # → PUT {"status": "in_queue"} (field omitted) → still False

async def test_detail_exposes_ivr_toggle(...):
    # GET /patient-forms/{id} → body["data"]["ivr_navigation_enabled"] mirrors the row
```

(Write both against the file's existing client/DB fixtures, mirroring `test_enqueue_stamps_enqueued_by_id`.)

- [ ] **Step 9: Update voice_lab to share the regex**

In `apps/control_plane/src/control_plane/api/v1/voice_lab.py`: delete the local `_E164 = re.compile(...)` (line 61) and the now-unused `import re`; add `from control_plane.queueability import E164_RE` and replace the one usage `_E164.match(body.phone_number)` with `E164_RE.match(body.phone_number)`.

- [ ] **Step 10: Fix existing integration fixtures**

`tests/integration/control_plane/test_call_queue.py` enqueue tests (`test_enqueue_form_triggers_dispatch`, `test_enqueue_blocked_transition_returns_422`, `test_enqueue_stamps_enqueued_by_id`, `test_dispatched_call_carries_queuer_as_owner`, `test_queue_started_call_is_publishable_by_queuer`) now hit the gate. Give their form fixtures a valid `insurance_provider_phone_number="+15551234567"` and seed a `livekit_outbound_trunk_id` integration credential (`{"trunk_id": "ST_test"}`) for the test tenant — follow how `tests/integration/control_plane/test_voice_lab.py` seeds the same credential for its outbound tests (reuse its helper/fixture if one exists; otherwise extract that seeding into `tests/integration/control_plane/conftest.py` as `seed_outbound_trunk(sessionmaker, kms, tenant_id)` and call it from both).

Also add one new integration test asserting the rejection shape:

```python
async def test_enqueue_rejected_without_payer_phone(app_client, seeded_form_without_phone):
    resp = await app_client.put(
        f"/api/v1/patient-forms/{seeded_form_without_phone}/status",
        json={"status": "in_queue"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert "phone" in body["message"].lower()
```

(Adapt the client/fixture names to the file's existing conventions — it already has an authenticated client fixture and form-seeding helpers; `seeded_form_without_phone` = the same form seed with `insurance_provider_phone_number=None`.)

- [ ] **Step 11: Run the gate**

Run: `just check`
Expected: PASS

- [ ] **Step 12: Commit**

```bash
git add -A vera-backend
git commit -m "feat(queue): queueability gate + per-form IVR navigation toggle on In_Queue"
```

---

### Task 3: Shared terminal-call-status helper

One function owns "a call reached a terminal status → drive the form edge (+ auto-retry)". Today this logic lives inside the doomed `POST /calls/{id}/status` endpoint; the dispatcher (dial failure) and the worker-event consumer both need it.

**Files:**
- Create: `packages/vera_core/src/vera_core/services/call_lifecycle.py`
- Test: `tests/unit/services/test_call_lifecycle.py`

**Interfaces:**
- Produces: `apply_terminal_call_status(call, form, status: CallStatus, *, tenant_max_retries: int) -> bool` — sets `call.current_status`, transitions the form (COMPLETED → `FormStatus.COMPLETED`; FAILED/NO_ANSWER/BUSY → `FormStatus.CALL_FAILED` with auto-retry to IN_QUEUE while retries remain). Returns `True` when the form was auto-requeued (caller must set `form.enqueued_at = func.now()`). Never raises on an illegal form edge (logs and leaves the form unchanged — the call's terminal status must still be recorded).

- [ ] **Step 1: Write the failing tests** — `tests/unit/services/test_call_lifecycle.py`

```python
"""apply_terminal_call_status — terminal call statuses drive the form lifecycle."""

from types import SimpleNamespace

from vera_core.models.enums import CallStatus, FormStatus
from vera_core.services.call_lifecycle import apply_terminal_call_status


def _call() -> SimpleNamespace:
    return SimpleNamespace(current_status=CallStatus.ACTIVE.value)


def _form(status: FormStatus = FormStatus.IN_CALL, retry_count: int = 0) -> SimpleNamespace:
    return SimpleNamespace(status=status.value, retry_count=retry_count, enqueued_at=None)


def test_completed_call_completes_form():
    call, form = _call(), _form()
    requeued = apply_terminal_call_status(call, form, CallStatus.COMPLETED, tenant_max_retries=3)
    assert call.current_status == CallStatus.COMPLETED.value
    assert form.status == FormStatus.COMPLETED.value
    assert requeued is False


def test_failed_call_auto_requeues_with_retries_remaining():
    call, form = _call(), _form(retry_count=0)
    requeued = apply_terminal_call_status(call, form, CallStatus.NO_ANSWER, tenant_max_retries=3)
    assert call.current_status == CallStatus.NO_ANSWER.value
    assert form.status == FormStatus.IN_QUEUE.value
    assert form.retry_count == 1
    assert requeued is True


def test_failed_call_stays_call_failed_when_retries_exhausted():
    call, form = _call(), _form(retry_count=3)
    requeued = apply_terminal_call_status(call, form, CallStatus.BUSY, tenant_max_retries=3)
    assert form.status == FormStatus.CALL_FAILED.value
    assert requeued is False


def test_illegal_form_edge_still_records_call_status():
    call, form = _call(), _form(status=FormStatus.COMPLETED)  # form already terminal
    requeued = apply_terminal_call_status(call, form, CallStatus.FAILED, tenant_max_retries=3)
    assert call.current_status == CallStatus.FAILED.value
    assert form.status == FormStatus.COMPLETED.value  # untouched
    assert requeued is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test tests/unit/services/test_call_lifecycle.py -v`
Expected: FAIL — `ModuleNotFoundError: vera_core.services.call_lifecycle`

- [ ] **Step 3: Create `packages/vera_core/src/vera_core/services/call_lifecycle.py`**

```python
"""Terminal call-status application — the one place a call's terminal status drives
the form lifecycle. Used by the queue dispatcher (dial failure) and the control
plane's worker-event consumer (call.ended / call.failed).

The form edge is best-effort by design: an illegal transition (e.g. a second call
racing the same form) must not prevent the call's terminal status from being
recorded — mirror of the old callback endpoint's contract.
"""

import contextlib
import logging
from typing import Any

from vera_core.models.enums import CallStatus, FormStatus
from vera_core.services.form_state_machine import FormStateMachine, InvalidTransitionError

logger = logging.getLogger(__name__)

_FORM_EDGE: dict[CallStatus, FormStatus] = {
    CallStatus.COMPLETED: FormStatus.COMPLETED,
    CallStatus.FAILED: FormStatus.CALL_FAILED,
    CallStatus.NO_ANSWER: FormStatus.CALL_FAILED,
    CallStatus.BUSY: FormStatus.CALL_FAILED,
}


def apply_terminal_call_status(
    call: Any, form: Any, status: CallStatus, *, tenant_max_retries: int
) -> bool:
    """Record *status* on *call* and drive *form*'s lifecycle edge.

    Returns True when the form was auto-requeued for retry — the caller owns
    `form.enqueued_at` (DB clock) in that case.
    """
    if status not in _FORM_EDGE:
        raise ValueError(f"{status.value} is not a terminal call status")
    call.current_status = status.value
    sm = FormStateMachine()
    requeued = False
    try:
        sm.transition(form, _FORM_EDGE[status], tenant_max_retries=tenant_max_retries)
        if _FORM_EDGE[status] is FormStatus.CALL_FAILED:
            # Auto-retry while retries remain; silently stay CALL_FAILED when exhausted.
            with contextlib.suppress(InvalidTransitionError):
                sm.transition(form, FormStatus.IN_QUEUE, tenant_max_retries=tenant_max_retries)
                requeued = True
    except InvalidTransitionError:
        logger.warning(
            "terminal call status '%s': form cannot leave '%s'; call status recorded, "
            "form left unchanged",
            status.value,
            form.status,
        )
    return requeued
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test tests/unit/services/test_call_lifecycle.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add -A vera-backend
git commit -m "feat(core): shared terminal-call-status helper driving the form lifecycle"
```

---

### Task 4: Dispatcher actually dials the payer

Port Voice Lab's outbound machinery into `try_dispatch`: resolve the tenant trunk, send real-call dispatch metadata, dial the form's payer number with 1 s pacing (Twilio ~1 CPS), and handle dial failure as a failed call with bounded retries.

**Files:**
- Modify: `packages/vera_core/src/vera_core/services/queue_dispatcher.py`
- Modify: callers of `try_dispatch` — `apps/control_plane/src/control_plane/api/v1/patient_forms.py:887`, `apps/control_plane/src/control_plane/api/v1/calls.py:534` (add the new `kms` argument; both endpoints already have / gain a `Kms` dep)
- Test: `tests/unit/services/test_queue_dispatcher.py` (extend), `tests/integration/control_plane/test_call_queue.py` (fixtures gain trunk + phone from Task 2)

**Interfaces:**
- Consumes: `apply_terminal_call_status` (Task 3), `OutboundDialError` (Task 1), `add_active_playbook_metadata` (Task 1), `get_integration_credentials`, `livekit.create_sip_participant(room_name, phone_number, trunk_id)`, `livekit.delete_room(room_name)`.
- Produces: `try_dispatch(session, tenant_id, livekit, kms, *, audit=None, dial_pacing_s: float = 1.0) -> int`. Dispatch metadata contract consumed by the worker: `{"wait_for_speaker": True, "publish_events": True, "enable_ivr_navigation": True?, "persona_tweak": {...}?, "ivr_playbook": {...}?}` — `enable_ivr_navigation`/`ivr_playbook` present only when `form.ivr_navigation_enabled` (Task 2's toggle; a missing key is the worker's generic default).

- [ ] **Step 1: Write the failing unit tests** — extend `tests/unit/services/test_queue_dispatcher.py`. Follow the file's existing fake-gateway/session conventions (it already fakes `create_call_room`); extend the fake with `create_sip_participant` and `delete_room` recorders. Add these tests:

```python
async def test_dispatch_dials_the_forms_payer_number(...):
    # form.insurance_provider_phone_number = "+15551234567"; trunk creds present
    # after try_dispatch: fake.sip_dials == [(room_name, "+15551234567", "ST_test")]
    # and dispatch metadata includes wait_for_speaker/publish_events True,
    # enable_ivr_navigation present iff form.ivr_navigation_enabled (assert both ways),
    # and persona tweak (when set on the tenant) is nested under metadata["persona_tweak"]

async def test_dispatch_without_trunk_leaves_forms_queued(...):
    # creds lookup returns None → 0 dispatched, form.status still IN_QUEUE, no room created

async def test_dial_failure_marks_call_failed_and_requeues_form(...):
    # fake.create_sip_participant raises OutboundDialError
    # → call.current_status == "failed", CallEvent(status=failed) recorded,
    #   room deleted, form back to IN_QUEUE with retry_count == 1, returns 0 dispatched

async def test_dials_are_paced_one_second_apart(...):
    # two queued forms, slots >= 2; monkeypatch asyncio.sleep to record delays
    # → exactly one sleep(1.0) recorded (between the two dials, none before the first)
```

Write them as real tests against the existing fakes in that file (the file already builds in-memory `PatientForm`/`Tenant` rows via its fixtures — mirror `test_...` functions already present for the room-creation path). Monkeypatch `vera_core.services.queue_dispatcher.get_integration_credentials` to return `{"trunk_id": "ST_test"}` (or `None` for the no-trunk test).

- [ ] **Step 2: Run to verify failure**

Run: `just test tests/unit/services/test_queue_dispatcher.py -v`
Expected: FAIL — `try_dispatch() missing required positional argument 'kms'` / attribute errors on the new asserts

- [ ] **Step 3: Rewrite the dispatch section of `queue_dispatcher.py`**

Add imports:

```python
import asyncio
import contextlib

from vera_core.integrations.credentials import get_integration_credentials
from vera_core.services.call_lifecycle import apply_terminal_call_status
from vera_core.services.ivr_selection import add_active_playbook_metadata
from vera_core.telephony import OutboundDialError
```

(and `KeyManagementService` under `TYPE_CHECKING` from `vera_core.config.kms`.)

Change the signature:

```python
async def try_dispatch(
    session: AsyncSession,
    tenant_id: UUID,
    livekit: Any,
    kms: Any,
    *,
    audit: AuditSink | None = None,
    dial_pacing_s: float = 1.0,
) -> int:
```

Change `_provider_in_hours` into a resolver the loop can reuse for the provider id and playbook:

```python
async def _resolve_provider(
    session: AsyncSession, form: PatientForm
) -> InsuranceProvider | None:
    """The form's ACTIVE insurance provider record, or None (working hours + playbook
    are then skipped — both are opt-in)."""
    if not form.insurance_provider:
        return None
    return (
        await session.execute(
            select(InsuranceProvider).where(
                InsuranceProvider.name == form.insurance_provider,
                InsuranceProvider.status == "active",
            )
        )
    ).scalar_one_or_none()
```

After the expiry pass and before the candidate loop, resolve the trunk once:

```python
    trunk_id: str | None = None
    if candidates:
        creds = await get_integration_credentials(
            session, kms, integration_type_name="livekit_outbound_trunk_id"
        )
        trunk_id = creds.get("trunk_id") if creds else None
        if not trunk_id:
            # The enqueue gate normally prevents this; config may have changed since.
            logger.warning(
                "dispatch: tenant %s has queued forms but no outbound trunk; leaving queued",
                tenant_id,
            )
            candidates = []
```

Replace the body of the candidate loop with (keep the surrounding `dispatched = 0`, tweak computation, and audit emission structure):

```python
    for form in candidates:
        provider = await _resolve_provider(session, form)
        if provider is not None and not is_within_working_hours(provider):
            continue

        call_mode = CallMode.RETRY if form.retry_count > 0 else CallMode.FULL
        # Real-call dispatch metadata: the worker must wait for the SIP callee to
        # answer and publish envelope events for live monitoring. IVR navigation is
        # the operator's per-form queue-time choice (voice-lab-style toggle) — when
        # ON, the provider's active playbook (if any) specializes the navigator.
        metadata: dict[str, Any] = {
            "wait_for_speaker": True,
            "publish_events": True,
        }
        if form.ivr_navigation_enabled:
            metadata["enable_ivr_navigation"] = True
        if tweak_fields := tweak.model_dump(exclude_none=True):
            metadata["persona_tweak"] = tweak_fields
        try:
            sm.transition(form, FormStatus.IN_CALL, tenant_max_retries=tenant.max_retries)
            async with session.begin_nested():
                call = Call(
                    tenant_id=tenant_id,
                    form_id=form.id,
                    current_status=CallStatus.INITIATED.value,
                    mode=call_mode.value,
                    initiated_by_id=form.enqueued_by_id,
                    insurance_provider_id=provider.id if provider else None,
                )
                session.add(call)
                await session.flush()
                room_name = room_name_for_call(tenant_id, call.id)
                if form.ivr_navigation_enabled and provider is not None:
                    await add_active_playbook_metadata(session, provider.id, metadata)
                await livekit.create_call_room(room_name, metadata=metadata)
                session.add(
                    CallEvent(
                        tenant_id=tenant_id,
                        call_id=call.id,
                        event_type=CallEventType.STATUS.value,
                        event_value=CallStatus.INITIATED.value,
                    )
                )
        except Exception:
            logger.exception(
                "dispatch: failed to dispatch form %s — reverting to IN_QUEUE", form.id
            )
            form.status = FormStatus.IN_QUEUE.value
            continue

        # Dial OUTSIDE the savepoint: a failed dial keeps the Call row as evidence
        # (FAILED + retry accounting) instead of rolling it back. Pace dials ~1/s
        # (Twilio CPS limit) — sleep between dials, never before the first.
        if dispatched > 0:
            await asyncio.sleep(dial_pacing_s)
        try:
            await livekit.create_sip_participant(
                room_name, form.insurance_provider_phone_number, trunk_id
            )
        except OutboundDialError:
            logger.warning("dispatch: outbound dial failed for call %s", call.id)
            with contextlib.suppress(Exception):  # room teardown is best-effort
                await livekit.delete_room(room_name)
            requeued = apply_terminal_call_status(
                call, form, CallStatus.FAILED, tenant_max_retries=tenant.max_retries
            )
            call.ended_at = func.now()
            if requeued:
                form.enqueued_at = func.now()
            session.add(
                CallEvent(
                    tenant_id=tenant_id,
                    call_id=call.id,
                    event_type=CallEventType.STATUS.value,
                    event_value=CallStatus.FAILED.value,
                )
            )
            continue

        dispatched += 1
        logger.info(
            "dispatch: initiated call %s for form %s (mode=%s)",
            call.id, form.id, call_mode.value,
        )
        if audit is not None:
            await audit.emit(
                AuditRecord(
                    tenant_id=tenant_id,
                    actor_type=ActorType.SYSTEM,
                    actor_label="queue-dispatcher",
                    event_type=AuditEvent.QUEUE_DISPATCH.value,
                    resource_type="patient_form",
                    resource_id=str(form.id),
                    detail={"call_id": str(call.id), "mode": call_mode.value},
                )
            )
```

Delete the old `_provider_in_hours` and the old pass-level `metadata = tweak.model_dump(...)` (the flat legacy persona shape — now nested per-call). Add `CallStatus`-needed imports if missing.

- [ ] **Step 4: Update the two call sites' signatures**

`patient_forms.py:887`: `await try_dispatch(session, tenant_id, livekit, kms, audit=audit)` (endpoint already has `kms: Kms` from Task 2 — this line is replaced again in Task 5; keep it compiling for now).
`calls.py:534` (`update_call_status`): add `kms: Kms` to the endpoint params and pass it: `await try_dispatch(session, tenant_id, livekit, kms, audit=audit)`. (This endpoint is deleted in Task 10; minimal edit here.)

- [ ] **Step 5: Run the new unit tests, then the whole gate**

Run: `just test tests/unit/services/test_queue_dispatcher.py -v` → PASS
Run: `just check` → PASS (integration `test_call_queue.py` passes because Task 2 seeded trunk + phone; its dispatch assertions may need the extra `create_sip_participant` recorded on the fake gateway used by the app fixture — extend that fake in `tests/integration/control_plane/conftest.py` the same way as the unit fake).

- [ ] **Step 6: Commit**

```bash
git add -A vera-backend
git commit -m "feat(dispatch): dial the payer via SIP with pacing, real-call metadata, and dial-failure retries"
```

---

### Task 5: Post-commit dispatch runner

Dial + 1 s pacing must not ride inside the HTTP request transaction (held row locks, request latency, orphaned rooms on rollback). Run the dispatch pass in its own committed session, strictly after the request's transaction commits.

> **EXECUTION DEVIATION (empirically verified):** the original mechanism here (FastAPI `BackgroundTasks`) is wrong for this repo's FastAPI 0.136 — a probe showed background tasks run BEFORE yield-dependency teardown (`dep-enter → handler-return → bg-ran → dep-exit`), i.e. before the `TenantSession` commit, so the pass saw an uncommitted, still-locked row and dispatched nothing (and any in-background wait for the commit would deadlock, since teardown runs only after background tasks finish). Built instead: `schedule_dispatch_pass(...)` spawns a **detached asyncio task** (strong-ref'd in a module `_PENDING` set) and `run_dispatch_pass` gains `wait_for_form_id` — its first statement is a plain (non-SKIP-LOCKED) `SELECT … FOR UPDATE` on the just-enqueued row, which Postgres queues behind the request's commit: a deterministic post-commit barrier with no polling. `drain_pending()` lets tests await detached dispatch work. The endpoint schedules with `wait_for_form_id=form_id`; the consumer/sweeper call `run_dispatch_pass` directly with no barrier (their transactions are already committed).

**Files:**
- Create: `apps/control_plane/src/control_plane/dispatch.py`
- Modify: `apps/control_plane/src/control_plane/api/v1/patient_forms.py` (schedule instead of await)
- Test: `tests/unit/control_plane/test_dispatch_runner.py` (new); `tests/integration/control_plane/test_call_queue.py` (existing enqueue→dispatch tests keep passing — TestClient executes background tasks before returning the response)

**Interfaces:**
- Produces: `run_dispatch_pass(sessionmaker, tenant_id: UUID, livekit, kms, audit) -> None` in `control_plane.dispatch` — opens `tenant_session`, runs `try_dispatch`, commits, swallows+logs all exceptions (a failed pass must never crash its host: background task or event consumer).

- [ ] **Step 1: Write the failing test** — `tests/unit/control_plane/test_dispatch_runner.py`

```python
"""run_dispatch_pass — a self-contained, exception-safe dispatch pass."""

from uuid import uuid4

import pytest

import control_plane.dispatch as dispatch_mod
from control_plane.dispatch import run_dispatch_pass


class _FakeSessionCtx:
    def __init__(self) -> None:
        self.session = object()

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_runs_try_dispatch_in_its_own_tenant_session(monkeypatch):
    seen: dict[str, object] = {}
    ctx = _FakeSessionCtx()
    monkeypatch.setattr(dispatch_mod, "tenant_session", lambda sm, tid: ctx)

    async def fake_try_dispatch(session, tenant_id, livekit, kms, *, audit=None):
        seen.update(session=session, tenant_id=tenant_id)
        return 1

    monkeypatch.setattr(dispatch_mod, "try_dispatch", fake_try_dispatch)
    tid = uuid4()
    await run_dispatch_pass(object(), tid, object(), object(), object())
    assert seen == {"session": ctx.session, "tenant_id": tid}


@pytest.mark.asyncio
async def test_swallows_and_logs_dispatch_errors(monkeypatch, caplog):
    monkeypatch.setattr(dispatch_mod, "tenant_session", lambda sm, tid: _FakeSessionCtx())

    async def boom(session, tenant_id, livekit, kms, *, audit=None):
        raise RuntimeError("livekit down")

    monkeypatch.setattr(dispatch_mod, "try_dispatch", boom)
    await run_dispatch_pass(object(), uuid4(), object(), object(), object())  # must not raise
    assert any("dispatch pass failed" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run to verify failure**

Run: `just test tests/unit/control_plane/test_dispatch_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: control_plane.dispatch`

- [ ] **Step 3: Create `apps/control_plane/src/control_plane/dispatch.py`**

```python
"""Self-contained dispatch pass, run OUTSIDE any request transaction.

The dispatcher makes external calls (LiveKit room + SIP dial) and sleeps between
dials for carrier pacing — none of that may hold an HTTP request's transaction or
row locks. Hosts: the status endpoint's post-commit background task and the
worker-event consumer (a call ended → a slot freed).
"""

import logging
from typing import TYPE_CHECKING, Any

from vera_core.db.rls import tenant_session
from vera_core.services.queue_dispatcher import try_dispatch

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from vera_core.audit import AuditSink

logger = logging.getLogger(__name__)


async def run_dispatch_pass(
    sessionmaker: "async_sessionmaker[AsyncSession]",
    tenant_id: "UUID",
    livekit: Any,
    kms: Any,
    audit: "AuditSink | None",
) -> None:
    """One dispatch pass in a fresh tenant-scoped session; commits on success.
    Exception-safe: a failed pass logs and returns — queued forms are retried on
    the next triggering event."""
    try:
        async with tenant_session(sessionmaker, tenant_id) as session:
            await try_dispatch(session, tenant_id, livekit, kms, audit=audit)
    except Exception:
        logger.exception("dispatch pass failed for tenant %s", tenant_id)
```

- [ ] **Step 4: Run to verify pass**

Run: `just test tests/unit/control_plane/test_dispatch_runner.py -v`
Expected: PASS

- [ ] **Step 5: Schedule it from the status endpoint**

In `apps/control_plane/src/control_plane/api/v1/patient_forms.py`:

- Add `BackgroundTasks` to the fastapi import: `from fastapi import APIRouter, BackgroundTasks, Query, Request, Response`.
- Add imports: `from control_plane.dispatch import run_dispatch_pass` and (already present) `get_sessionmaker`.
- Remove `from vera_core.services.queue_dispatcher import try_dispatch`.
- Add `background: BackgroundTasks,` to `update_patient_form_status`'s parameters.
- Replace the tail dispatcher block:

```python
    # Kick a dispatch pass AFTER this transaction commits (yield-dependency teardown
    # runs before background tasks) — dial + pacing must not ride the request
    # transaction. The response acknowledges the manual transition only; clients
    # observe dispatch via the calls list.
    if target == FormStatus.IN_QUEUE:
        background.add_task(
            run_dispatch_pass, get_sessionmaker(request), tenant_id, livekit, kms, audit
        )
```

- [ ] **Step 6: Run the gate**

Run: `just check`
Expected: PASS (`test_call_queue.py` enqueue→dispatch tests still pass: the test client runs background tasks within the request call)

- [ ] **Step 7: Commit**

```bash
git add -A vera-backend
git commit -m "feat(dispatch): run the dispatch pass post-commit as a background task"
```

---

### Task 6: New worker lifecycle events (`call.answered`, `call.ended`)

**Files:**
- Modify: `packages/vera_core/src/vera_core/events/worker.py`
- Test: extend the existing worker-event parse tests in `tests/unit/events/` (the directory exists; add to its test module for `worker.py`, creating `tests/unit/events/test_worker_events.py` if none covers parsing)

**Interfaces:**
- Produces: `CallAnsweredEvent(type="call.answered", room_name, ts)`, `CallEndedEvent(type="call.ended", room_name, ts)`; `WorkerEvent = CallFailedEvent | CallAnsweredEvent | CallEndedEvent` with a `type`-discriminated `parse_worker_event`.

- [ ] **Step 1: Write the failing tests** (in `tests/unit/events/test_worker_events.py`, alongside any existing ones)

```python
from vera_core.events import (
    CallAnsweredEvent,
    CallEndedEvent,
    CallFailedEvent,
    parse_worker_event,
)


def test_parse_call_answered_roundtrip():
    ev = CallAnsweredEvent(room_name="call--t--c", ts=123)
    assert parse_worker_event(ev.model_dump_json()) == ev


def test_parse_call_ended_roundtrip():
    ev = CallEndedEvent(room_name="call--t--c", ts=456)
    assert parse_worker_event(ev.model_dump_json()) == ev


def test_parse_still_handles_call_failed():
    raw = '{"type": "call.failed", "room_name": "r", "reason": "no_answer", "ts": 1}'
    ev = parse_worker_event(raw)
    assert isinstance(ev, CallFailedEvent)
```

- [ ] **Step 2: Run to verify failure**

Run: `just test tests/unit/events/ -v`
Expected: FAIL — `ImportError: CallAnsweredEvent`

- [ ] **Step 3: Extend `packages/vera_core/src/vera_core/events/worker.py`**

Add after `CallFailedEvent` (imports: add `Annotated` to typing, `Field` to pydantic):

```python
class CallAnsweredEvent(BaseModel):
    """Emitted when the SIP callee answered — the call is live."""

    type: Literal["call.answered"] = "call.answered"
    room_name: str
    ts: int  # epoch milliseconds


class CallEndedEvent(BaseModel):
    """Emitted from the worker's shutdown callback — the session finished after
    the call was live (hangup by either side, or the agent's end_call tool)."""

    type: Literal["call.ended"] = "call.ended"
    room_name: str
    ts: int  # epoch milliseconds


type WorkerEvent = CallFailedEvent | CallAnsweredEvent | CallEndedEvent
_ADAPTER: TypeAdapter[WorkerEvent] = TypeAdapter(
    Annotated[
        CallFailedEvent | CallAnsweredEvent | CallEndedEvent,
        Field(discriminator="type"),
    ]
)
```

(Replace the old single-type `WorkerEvent` alias, `_ADAPTER`, and the "widen to a Union later" comment.) Export the two new names from `packages/vera_core/src/vera_core/events/__init__.py` next to `CallFailedEvent`.

- [ ] **Step 4: Run to verify pass, then the gate**

Run: `just test tests/unit/events/ -v` → PASS; `just check` → PASS

- [ ] **Step 5: Commit**

```bash
git add -A vera-backend
git commit -m "feat(events): add call.answered and call.ended worker events"
```

---

### Task 7: Consumer closes the loop (Call + PatientForm rows, refill slots)

The `WorkerEventConsumer` becomes the single writer of worker-driven call/form state: `call.answered` → ACTIVE + `started_at`; `call.ended` → COMPLETED + form COMPLETED; `call.failed` → mapped terminal status + form CALL_FAILED/auto-retry + room teardown. Every terminal event ends with a dispatch pass (a slot freed). Rooms with no Call row (Voice Lab's synthetic ids) skip all DB work.

**Files:**
- Create: `apps/control_plane/src/control_plane/call_closeout.py` (shared terminal-closeout — the sweeper in Task 13 reuses it)
- Modify: `apps/control_plane/src/control_plane/worker_events.py`
- Modify: `apps/control_plane/src/control_plane/main.py:136-142` (constructor args)
- Test: `tests/unit/control_plane/test_worker_events.py` (extend — it already fakes redis/livekit for the consumer)

**Interfaces:**
- Consumes: `apply_terminal_call_status` (Task 3), `run_dispatch_pass` (Task 5), `CallAnsweredEvent`/`CallEndedEvent` (Task 6), `parse_room_name` → `RoomRef(tenant_id, call_id)`.
- Produces: `WorkerEventConsumer(redis, livekit, sessionmaker, kms, audit, *, block_ms=..., reclaim_idle_ms=..., teardown_grace_ms=..., consumer_name=None)` — three new required positional deps; `close_call(sessionmaker, audit, room_name, status: CallStatus, *, trigger: str, actor_label: str = "agent-worker") -> RoomRef | None` in `control_plane.call_closeout` (returns the RoomRef when a slot was freed — the caller then runs a dispatch pass; None for voice-lab rooms / already-terminal calls).

- [ ] **Step 1: Write the failing tests** — extend `tests/unit/control_plane/test_worker_events.py`. Follow its existing fake patterns (it already constructs the consumer and feeds events). The DB seam is easiest to test through an injected sessionmaker backed by the test DB when the file already uses one; if it is pure-unit (fakes only), monkeypatch `worker_events.tenant_session` and use `SimpleNamespace` rows like Task 3's tests. Cover:

```python
async def test_call_answered_activates_call_and_stamps_started_at(...):
    # seed Call(current_status="initiated") whose id matches the room name
    # handle CallAnsweredEvent → current_status == "active", started_at not None,
    # CallEvent(status=active) added

async def test_call_ended_completes_call_and_form_then_dispatches(...):
    # Call(active) + PatientForm(in_call) → CallEndedEvent
    # → call completed + ended_at set, form completed, CallEvent added,
    #   FORM_STATUS_CHANGE audit emitted, run_dispatch_pass invoked once

async def test_call_failed_maps_reason_updates_rows_and_tears_room_down(...):
    # CallFailedEvent(reason=no_answer) → call "no_answer", form auto-requeued
    #   (retry_count 1, enqueued_at set), room metadata set + room deleted (existing
    #   teardown behavior preserved), run_dispatch_pass invoked

async def test_events_for_rooms_without_call_row_touch_no_db(...):
    # voice-lab style room (unknown call id) → handlers return after lookup;
    #   call.failed still does the metadata+delete_room teardown

async def test_terminal_events_are_idempotent(...):
    # CallEndedEvent on an already-completed call → no row changes, no dispatch
```

- [ ] **Step 2: Run to verify failure**

Run: `just test tests/unit/control_plane/test_worker_events.py -v`
Expected: FAIL — constructor signature / missing handlers

- [ ] **Step 3: Create `apps/control_plane/src/control_plane/call_closeout.py`**

```python
"""Shared terminal closeout for a call room: record the call's terminal status,
drive the form edge (with bounded auto-retry), audit the worker-driven form
change. The two writers of worker-driven terminal state — the worker-event
consumer (call.ended / call.failed) and the pipeline sweeper (stuck-call
reconciliation) — both go through here, so the semantics can never diverge.

Idempotent by construction: rooms without a Call row (Voice Lab's synthetic ids)
and already-terminal calls return None untouched, so redeliveries and
consumer/sweeper races are harmless (the row lock serializes them).
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.audit import AuditRecord, AuditSink
from vera_core.db.rls import tenant_session
from vera_core.models import Call, CallEvent, PatientForm, Tenant
from vera_core.models.audit_log import ActorType, AuditEvent
from vera_core.models.call import TERMINAL_CALL_STATUSES
from vera_core.models.enums import CallEventType, CallStatus
from vera_core.observability.correlation import RoomRef, parse_room_name
from vera_core.services.call_lifecycle import apply_terminal_call_status

logger = logging.getLogger(__name__)

TERMINAL_VALUES = frozenset(s.value for s in TERMINAL_CALL_STATUSES)


async def close_call(
    sessionmaker: async_sessionmaker[AsyncSession],
    audit: AuditSink,
    room_name: str,
    status: CallStatus,
    *,
    trigger: str,
    actor_label: str = "agent-worker",
) -> RoomRef | None:
    """Apply *status* as the call's terminal state. Returns the RoomRef when a
    concurrency slot was freed (caller should run a dispatch pass), else None."""
    ref = parse_room_name(room_name)
    if ref is None:
        return None
    async with tenant_session(sessionmaker, ref.tenant_id) as session:
        call = (
            await session.execute(
                select(Call).where(Call.id == ref.call_id).with_for_update()
            )
        ).scalar_one_or_none()
        if call is None or call.current_status in TERMINAL_VALUES:
            return None  # voice-lab room, or idempotent redelivery / lost race
        form = (
            await session.execute(
                select(PatientForm).where(PatientForm.id == call.form_id).with_for_update()
            )
        ).scalar_one_or_none()
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == ref.tenant_id))
        ).scalar_one()
        previous_form_status = form.status if form is not None else None
        if form is not None:
            requeued = apply_terminal_call_status(
                call, form, status, tenant_max_retries=tenant.max_retries
            )
            if requeued:
                form.enqueued_at = func.now()
        else:  # form deleted out from under the call — still record the call status
            call.current_status = status.value
        call.ended_at = func.now()
        session.add(
            CallEvent(
                tenant_id=ref.tenant_id,
                call_id=call.id,
                event_type=CallEventType.STATUS.value,
                event_value=status.value,
            )
        )
        if form is not None and form.status != previous_form_status:
            await audit.emit(
                AuditRecord(
                    tenant_id=ref.tenant_id,
                    actor_type=ActorType.SERVICE,
                    actor_user_id=None,
                    actor_label=actor_label,
                    event_type=AuditEvent.FORM_STATUS_CHANGE.value,
                    resource_type="patient_form",
                    resource_id=str(form.id),
                    detail={
                        "from": previous_form_status,
                        "to": form.status,
                        "call_id": str(call.id),
                        "trigger": trigger,
                    },
                )
            )
    return ref
```

- [ ] **Step 4: Implement the consumer in `worker_events.py`**

New imports:

```python
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.call_closeout import TERMINAL_VALUES, close_call
from control_plane.dispatch import run_dispatch_pass
from vera_core.audit import AuditSink
from vera_core.db.rls import tenant_session
from vera_core.events import (  # keep the module's existing event imports too
    CallAnsweredEvent,
    CallEndedEvent,
    CallFailedEvent,
    CallFailureReason,
)
from vera_core.models import Call, CallEvent
from vera_core.models.enums import CallEventType, CallStatus
```

Module constants:

```python
_FAILURE_STATUS: dict[CallFailureReason, CallStatus] = {
    CallFailureReason.NO_ANSWER: CallStatus.NO_ANSWER,
    CallFailureReason.BUSY_OR_DECLINED: CallStatus.BUSY,
    CallFailureReason.FAILED: CallStatus.FAILED,
}
```

Constructor — add the three deps and register the new handlers:

```python
    def __init__(
        self,
        redis: Redis,
        livekit: LiveKitGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        kms: Any,
        audit: AuditSink,
        *,
        block_ms: int = 5_000,
        reclaim_idle_ms: int = 60_000,
        teardown_grace_ms: int = 1_500,
        consumer_name: str | None = None,
    ) -> None:
        ...  # existing assignments stay
        self._sessionmaker = sessionmaker
        self._kms = kms
        self._audit = audit
        self._handlers: dict[str, EventHandler] = {
            "call.failed": self._handle_call_failed,
            "call.answered": self._handle_call_answered,
            "call.ended": self._handle_call_ended,
        }
```

New handlers + helpers (the DB seam of every handler; `call.failed` keeps its existing teardown first, then adds the DB part):

```python
    async def _handle_call_answered(self, event: WorkerEvent) -> None:
        if not isinstance(event, CallAnsweredEvent):
            return
        ref = parse_room_name(event.room_name)
        if ref is None:
            return
        async with tenant_session(self._sessionmaker, ref.tenant_id) as session:
            call = (
                await session.execute(
                    select(Call).where(Call.id == ref.call_id).with_for_update()
                )
            ).scalar_one_or_none()
            if call is None or call.current_status in TERMINAL_VALUES:
                return  # voice-lab room, or a stale redelivery after terminal
            if call.current_status == CallStatus.ACTIVE.value:
                return  # idempotent redelivery
            call.current_status = CallStatus.ACTIVE.value
            call.started_at = func.now()
            session.add(
                CallEvent(
                    tenant_id=ref.tenant_id,
                    call_id=call.id,
                    event_type=CallEventType.STATUS.value,
                    event_value=CallStatus.ACTIVE.value,
                )
            )

    async def _handle_call_ended(self, event: WorkerEvent) -> None:
        if not isinstance(event, CallEndedEvent):
            return
        await self._close_and_refill(event.room_name, CallStatus.COMPLETED, trigger="call.ended")

    async def _handle_call_failed(self, event: WorkerEvent) -> None:
        if not isinstance(event, CallFailedEvent):
            return
        if parse_room_name(event.room_name) is None:
            logger.warning("call.failed for non-vera room %s; ignoring", event.room_name)
            return
        logger.info(
            "call.failed room=%s reason=%s: setting metadata + deleting room",
            event.room_name,
            event.reason.value,
        )
        await self._livekit.set_room_metadata(
            event.room_name, {"status": "call_failed", "reason": event.reason.value}
        )
        # Let the RoomMetadataChanged frame reach the browser before teardown.
        if self._teardown_grace_ms:
            await asyncio.sleep(self._teardown_grace_ms / 1000)
        await self._livekit.delete_room(event.room_name)
        await self._close_and_refill(
            event.room_name, _FAILURE_STATUS[event.reason], trigger="call.failed"
        )

    async def _close_and_refill(
        self, room_name: str, status: CallStatus, *, trigger: str
    ) -> None:
        """Terminal closeout via the shared path, then refill the freed slot
        (dispatch runs AFTER close_call's transaction committed)."""
        ref = await close_call(
            self._sessionmaker, self._audit, room_name, status, trigger=trigger
        )
        if ref is not None:
            await run_dispatch_pass(
                self._sessionmaker, ref.tenant_id, self._livekit, self._kms, self._audit
            )
```

(`parse_room_name` stays imported from `vera_core.observability.correlation` as today.)

- [ ] **Step 5: Wire the new deps in `main.py`**

At `main.py:136`, pass the new args:

```python
            consumer = WorkerEventConsumer(
                worker_events_redis,
                app.state.livekit,
                sessionmaker,
                app.state.kms,
                app.state.audit,
                block_ms=settings.worker_events_block_ms,
                reclaim_idle_ms=settings.worker_events_reclaim_idle_ms,
                teardown_grace_ms=settings.call_failed_teardown_grace_ms,
            )
```

(`sessionmaker` is the local variable already used for `DatabaseAuditWriter(sessionmaker)`; `app.state.kms` is set earlier in `create_app`.)

- [ ] **Step 6: Run tests, then the gate**

Run: `just test tests/unit/control_plane/test_worker_events.py -v` → PASS
Run: `just check` → PASS

- [ ] **Step 7: Commit**

```bash
git add -A vera-backend
git commit -m "feat(consumer): close the call loop — worker events update call/form rows and refill slots"
```

---

### Task 8: Worker emits lifecycle events

The worker signals `call.answered` when the SIP callee answers and `call.ended` from its shutdown callback, over the existing bus. One Redis client + bus per job, created for canonical vera rooms.

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/main.py`
- Test: `tests/unit/agent_worker/test_lifecycle_events.py` (new; the `tests/unit/agent_worker/` package exists)

**Interfaces:**
- Consumes: `WorkerEventBus.emit`, `CallAnsweredEvent`, `CallEndedEvent` (Task 6).
- Produces: dispatch-metadata-independent behavior — every canonical room emits `call.ended` on shutdown; `call.answered` only when a SIP participant becomes the ready speaker.

- [ ] **Step 1: Write the failing test** — `tests/unit/agent_worker/test_lifecycle_events.py`

The entrypoint is heavily framework-bound; test the extracted pure helper instead. The implementation (Step 3) extracts `_lifecycle_events(bus, room_name)` → a small emitter object; test that:

```python
"""CallLifecycleEmitter — the worker's answered/ended signals to the control plane."""

import pytest

from agent_worker.main import CallLifecycleEmitter
from vera_core.events import CallAnsweredEvent, CallEndedEvent


class _FakeBus:
    def __init__(self) -> None:
        self.emitted = []

    async def emit(self, event) -> None:
        self.emitted.append(event)


@pytest.mark.asyncio
async def test_answered_then_ended_emit_in_order():
    bus = _FakeBus()
    emitter = CallLifecycleEmitter(bus, "call--t--c")
    await emitter.answered(now_ms=100)
    await emitter.ended(now_ms=200)
    assert isinstance(bus.emitted[0], CallAnsweredEvent)
    assert isinstance(bus.emitted[1], CallEndedEvent)
    assert bus.emitted[0].room_name == "call--t--c"
    assert bus.emitted[0].ts == 100


@pytest.mark.asyncio
async def test_emit_failures_never_raise():
    class _Boom:
        async def emit(self, event) -> None:
            raise RuntimeError("redis down")

    emitter = CallLifecycleEmitter(_Boom(), "call--t--c")
    await emitter.answered(now_ms=1)  # must not raise — never break a live call
    await emitter.ended(now_ms=2)
```

- [ ] **Step 2: Run to verify failure**

Run: `just test tests/unit/agent_worker/test_lifecycle_events.py -v`
Expected: FAIL — `ImportError: CallLifecycleEmitter`

- [ ] **Step 3: Implement in `apps/agent_worker/src/agent_worker/main.py`**

Add to imports: `CallAnsweredEvent, CallEndedEvent` from `vera_core.events`.

Add the emitter class (after `_emit_call_failed`):

```python
class CallLifecycleEmitter:
    """Best-effort lifecycle signals to the control plane. A bus failure must never
    break a live call — log and continue (mirrors the transcript publisher's posture)."""

    def __init__(self, bus: WorkerEventBus, room_name: str) -> None:
        self._bus = bus
        self._room_name = room_name

    async def answered(self, *, now_ms: int) -> None:
        await self._emit(CallAnsweredEvent(room_name=self._room_name, ts=now_ms))

    async def ended(self, *, now_ms: int) -> None:
        await self._emit(CallEndedEvent(room_name=self._room_name, ts=now_ms))

    async def _emit(self, event: CallAnsweredEvent | CallEndedEvent) -> None:
        try:
            await self._bus.emit(event)
        except Exception:
            logger.exception("failed to emit %s for %s", event.type, self._room_name)
```

Restructure `entrypoint` to hold one events Redis/bus per canonical room (replacing the ad-hoc `failure_redis` in the CallFailed branch):

```python
    # One worker-event bus per job for canonical rooms (real calls AND voice-lab
    # rooms — the consumer no-ops when no Call row exists). Foreign/console rooms
    # get none.
    events_redis: Redis | None = None
    lifecycle: CallLifecycleEmitter | None = None
    if parse_room_name(room_name) is not None:
        events_redis = create_redis(settings.redis_url)
        bus = WorkerEventBus(events_redis, maxlen=settings.worker_events_stream_maxlen)
        lifecycle = CallLifecycleEmitter(bus, room_name)
```

Place this right after the `resolve_session` guard. In the `wait_for_speaker` block:

- The `CallFailed` branch: replace the `failure_redis` creation with the shared bus, and close `events_redis` before returning:

```python
        if isinstance(outcome, CallFailed):
            logger.warning("outbound call failed for room %s: %s", room_name, outcome.reason.value)
            if events_redis is not None:
                bus = WorkerEventBus(events_redis, maxlen=settings.worker_events_stream_maxlen)
                await _emit_call_failed(
                    bus, room_name, outcome.reason, now_ms=int(time.time() * 1000)
                )
                await events_redis.aclose()
            return
        speaker = outcome.participant
        # The SIP callee answering is the "call is live" signal; a browser caller
        # (voice-lab browser mode) is not an answered phone call.
        if (
            lifecycle is not None
            and speaker.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        ):
            await lifecycle.answered(now_ms=int(time.time() * 1000))
```

In `_on_shutdown`, after `await boundary.close_session(session_id)` append:

```python
        # Last: signal call end (the consumer completes the form and refills the
        # slot), then release the events client. A hard worker crash skips this —
        # the control plane's pipeline sweeper reconciles that case (room gone +
        # call still non-terminal → failed).
        if lifecycle is not None:
            await lifecycle.ended(now_ms=int(time.time() * 1000))
        if events_redis is not None:
            try:
                await events_redis.aclose()
            except Exception:
                logger.exception("failed to close events redis for %s", room_name)
```

- [ ] **Step 4: Run tests + gate**

Run: `just test tests/unit/agent_worker/ -v` → PASS; `just check` → PASS

- [ ] **Step 5: Commit**

```bash
git add -A vera-backend
git commit -m "feat(worker): emit call.answered/call.ended lifecycle events"
```

---

### Task 9: Generalized per-call event stream + worker publishing

A new envelope stream (`{type, data, ts}`) per room — transcript turns today, call-status frames now, form-fill events later (other developer). The worker publishes into it when dispatched with `publish_events: true` (the dispatcher's flag from Task 4). Voice Lab's transcript stream is untouched.

**Files:**
- Create: `packages/vera_core/src/vera_core/call_stream.py`
- Modify: `apps/agent_worker/src/agent_worker/transcript_publisher.py` (accept a `TurnPublisher` protocol)
- Modify: `apps/agent_worker/src/agent_worker/main.py` (publish under `publish_events`)
- Test: `tests/unit/transcript/test_call_stream.py` (new)

**Interfaces:**
- Produces: in `vera_core.call_stream` —
  - `CallStreamEvent(BaseModel)`: `type: str`, `data: dict[str, Any]`, `ts: int`
  - `call_stream_key(room_name) -> str` (`"vera:call-events:" + room_name`)
  - `RedisCallStreamStore(redis, *, ttl_seconds, end_grace_seconds, block_ms=5000)` — `publish/mark_ended/delete/read`, same sentinel semantics as `RedisTranscriptStore`
  - `CallStreamService(store)` — `publish_turn(room_name, role, text, *, ts)` (envelope `type="transcript"`, `data={"role": ..., "text": ...}`), `publish_status(room_name, status: str, *, ts)` (envelope `type="call_status"`, `data={"status": ...}`), `consume(room_name)`, `end(room_name)`, `clear(room_name)`
- Modifies: `attach_transcript_publisher(session, service: TurnPublisher, room_name)` — `TurnPublisher` protocol satisfied by both `TranscriptService` and `CallStreamService`.

- [ ] **Step 1: Write the failing tests** — `tests/unit/transcript/test_call_stream.py`

```python
"""CallStreamEvent envelope + service semantics (in-memory store variant)."""

import pytest

from vera_core.call_stream import CallStreamEvent, CallStreamService, call_stream_key


class _MemStore:
    """Minimal in-memory store capturing publishes; read replays then stops on end."""

    def __init__(self) -> None:
        self.events: list[CallStreamEvent] = []
        self.ended = False

    async def publish(self, room_name: str, event: CallStreamEvent) -> None:
        self.events.append(event)

    async def mark_ended(self, room_name: str) -> None:
        self.ended = True

    async def delete(self, room_name: str) -> None:
        self.events.clear()

    async def read(self, room_name: str):
        for i, event in enumerate(self.events):
            yield (f"{i}-0", event)


def test_key_prefix():
    assert call_stream_key("call--t--c") == "vera:call-events:call--t--c"


@pytest.mark.asyncio
async def test_publish_turn_wraps_transcript_envelope():
    store = _MemStore()
    svc = CallStreamService(store)
    await svc.publish_turn("r", "agent", "hello", ts=42)
    assert store.events == [
        CallStreamEvent(type="transcript", data={"role": "agent", "text": "hello"}, ts=42)
    ]


@pytest.mark.asyncio
async def test_publish_status_wraps_call_status_envelope():
    store = _MemStore()
    svc = CallStreamService(store)
    await svc.publish_status("r", "active", ts=7)
    assert store.events == [
        CallStreamEvent(type="call_status", data={"status": "active"}, ts=7)
    ]


@pytest.mark.asyncio
async def test_consume_yields_envelope_events():
    store = _MemStore()
    svc = CallStreamService(store)
    await svc.publish_turn("r", "user", "hi", ts=1)
    got = [e async for _id, e in svc.consume("r")]
    assert got[0].type == "transcript" and got[0].data["text"] == "hi"
```

Also add a Redis round-trip test in the same file mirroring how `tests/unit/transcript/` (or `tests/integration/transcript/`) tests `RedisTranscriptStore` — copy that file's fixture approach for a fake/real redis; assert publish → read yields the envelope and the ended sentinel terminates `read`.

- [ ] **Step 2: Run to verify failure**

Run: `just test tests/unit/transcript/test_call_stream.py -v`
Expected: FAIL — `ModuleNotFoundError: vera_core.call_stream`

- [ ] **Step 3: Create `packages/vera_core/src/vera_core/call_stream.py`**

```python
"""Generalized live per-call event stream — envelope model, Redis transport, service.

The real-call counterpart of `vera_core.transcript` (which stays voice-lab-only):
one stream per room carrying typed envelopes so ONE SSE can deliver every live
surface — transcript turns today, call-status frames, and (later) form-filling
progress — without a new pipe per event type. Payloads are tokenized /
de-identified only (same PHI contract as the transcript stream); never hydrated
raw PHI.
"""

import json
from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError

_KEY_PREFIX = "vera:call-events:"
_ENDED_FIELD = "event"
_ENDED_VALUE = "ended"

TYPE_TRANSCRIPT = "transcript"
TYPE_CALL_STATUS = "call_status"


def call_stream_key(room_name: str) -> str:
    return f"{_KEY_PREFIX}{room_name}"


class CallStreamEvent(BaseModel):
    """One live event. `data` is type-specific and de-identified by construction."""

    type: str  # "transcript" | "call_status" | future types (e.g. "form_field")
    data: dict[str, Any]
    ts: int  # epoch milliseconds


class CallStreamStore(Protocol):
    async def publish(self, room_name: str, event: CallStreamEvent) -> None: ...
    async def mark_ended(self, room_name: str) -> None: ...
    async def delete(self, room_name: str) -> None: ...
    def read(self, room_name: str) -> AsyncIterator[tuple[str, CallStreamEvent]]: ...


class RedisCallStreamStore:
    """Redis Streams transport; identical lifecycle to RedisTranscriptStore
    (rolling backstop TTL on publish; ended sentinel + grace TTL; replay-then-tail
    read that stops on the sentinel or a vanished key)."""

    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int,
        end_grace_seconds: int,
        block_ms: int = 5000,
    ) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._end_grace_seconds = end_grace_seconds
        self._block_ms = block_ms

    async def publish(self, room_name: str, event: CallStreamEvent) -> None:
        key = call_stream_key(room_name)
        pipe = self._redis.pipeline(transaction=False)
        pipe.xadd(
            key, {"type": event.type, "data": json.dumps(event.data), "ts": str(event.ts)}
        )
        pipe.expire(key, self._ttl_seconds)
        await pipe.execute()

    async def mark_ended(self, room_name: str) -> None:
        key = call_stream_key(room_name)
        pipe = self._redis.pipeline(transaction=False)
        pipe.xadd(key, {_ENDED_FIELD: _ENDED_VALUE})
        pipe.expire(key, self._end_grace_seconds)
        await pipe.execute()

    async def delete(self, room_name: str) -> None:
        await self._redis.delete(call_stream_key(room_name))

    async def read(self, room_name: str) -> AsyncIterator[tuple[str, CallStreamEvent]]:
        key = call_stream_key(room_name)
        last_id = "0"
        seen = False
        while True:
            try:
                result = await self._redis.xread({key: last_id}, block=self._block_ms)
            except RedisTimeoutError:
                # BLOCK with no entries RAISES (per-command read deadline) — idle tick.
                result = None
            if not result:
                if seen and not await self._redis.exists(key):
                    return
                continue
            seen = True
            xread_result = cast(
                "list[tuple[str, list[tuple[str, dict[str, str]]]]]", result
            )
            _stream, entries = xread_result[0]
            for entry_id, fields in entries:
                last_id = entry_id
                if fields.get(_ENDED_FIELD) == _ENDED_VALUE:
                    return
                yield (
                    entry_id,
                    CallStreamEvent(
                        type=fields["type"],
                        data=json.loads(fields["data"]),
                        ts=int(fields["ts"]),
                    ),
                )


class CallStreamService:
    """Produce/consume surface over a CallStreamStore — no caller touches raw Redis.
    `publish_turn` matches the transcript publisher's TurnPublisher protocol so the
    worker's ReorderingEmitter can feed either stream."""

    def __init__(self, store: CallStreamStore) -> None:
        self._store = store

    async def publish_turn(
        self, room_name: str, role: Literal["user", "agent"], text: str, *, ts: int
    ) -> None:
        await self._store.publish(
            room_name,
            CallStreamEvent(type=TYPE_TRANSCRIPT, data={"role": role, "text": text}, ts=ts),
        )

    async def publish_status(self, room_name: str, status: str, *, ts: int) -> None:
        await self._store.publish(
            room_name, CallStreamEvent(type=TYPE_CALL_STATUS, data={"status": status}, ts=ts)
        )

    def consume(self, room_name: str) -> AsyncIterator[tuple[str, CallStreamEvent]]:
        return self._store.read(room_name)

    async def end(self, room_name: str) -> None:
        await self._store.mark_ended(room_name)

    async def clear(self, room_name: str) -> None:
        await self._store.delete(room_name)
```

- [ ] **Step 4: Widen the transcript publisher's service type**

In `apps/agent_worker/src/agent_worker/transcript_publisher.py`: replace `from vera_core.transcript import ROLE_AGENT, ROLE_USER, TranscriptService` with:

```python
from typing import Any, Literal, Protocol

from vera_core.transcript import ROLE_AGENT, ROLE_USER


class TurnPublisher(Protocol):
    """Anything that can receive ordered finalized turns (TranscriptService for the
    voice-lab stream; CallStreamService for the real-call envelope stream)."""

    async def publish_turn(
        self, room_name: str, role: Literal["user", "agent"], text: str, *, ts: int
    ) -> None: ...
```

and change both annotations `service: TranscriptService` → `service: TurnPublisher` (in `ReorderingEmitter.__init__` and `attach_transcript_publisher`).

- [ ] **Step 5: Publish from the worker under `publish_events`**

In `apps/agent_worker/src/agent_worker/main.py`, add imports:

```python
from vera_core.call_stream import CallStreamService, RedisCallStreamStore
```

After the existing `publish_transcript` block (which stays for Voice Lab), add the real-call block:

```python
    # Real-call envelope stream (dispatcher opt-in via publish_events): transcript
    # turns + call_status frames for the /calls/{id}/events SSE. Reuses the
    # transcript TTL settings — same lifecycle, different stream.
    call_stream_redis: Redis | None = None
    call_stream: CallStreamService | None = None
    call_stream_emitter: ReorderingEmitter | None = None
    if meta.get("publish_events"):
        call_stream_redis = create_redis(settings.redis_url)
        call_stream = CallStreamService(
            RedisCallStreamStore(
                call_stream_redis,
                ttl_seconds=settings.transcript_stream_ttl_seconds,
                end_grace_seconds=settings.transcript_end_grace_seconds,
            )
        )
        call_stream_emitter = attach_transcript_publisher(session, call_stream, room_name)
        if speaker is not None:  # the callee already answered during wait_for_speaker
            await call_stream.publish_status(room_name, "active", ts=int(time.time() * 1000))
```

(Place it after `session = build_session(...)` since `attach_transcript_publisher` needs the AgentSession.)

Extend `_on_shutdown` — insert BEFORE `await boundary.close_session(session_id)` (mirroring the transcript block's flush-before-end ordering):

```python
        if call_stream_emitter is not None:
            try:
                await call_stream_emitter.aclose()
            except Exception:
                logger.exception("failed to flush call stream for %s", room_name)
        if call_stream is not None:
            try:
                await call_stream.publish_status(room_name, "ended", ts=int(time.time() * 1000))
                await call_stream.end(room_name)
            except Exception:
                logger.exception("failed to end call stream for %s", room_name)
        if call_stream_redis is not None:
            try:
                await call_stream_redis.aclose()
            except Exception:
                logger.exception("failed to close call stream redis for %s", room_name)
```

- [ ] **Step 6: Run tests + gate**

Run: `just test tests/unit/transcript/test_call_stream.py tests/unit/agent_worker/ -v` → PASS
Run: `just check` → PASS

- [ ] **Step 7: Commit**

```bash
git add -A vera-backend
git commit -m "feat(stream): per-call envelope event stream; worker publishes transcript + status frames"
```

---

### Task 10: `GET /calls/{call_id}/events` SSE

The real-call SSE: calls:read + the join-token visibility rule (owner OR published OR ownerless; revoked → 404), audited, DB connection released before streaming.

**Files:**
- Modify: `apps/control_plane/src/control_plane/deps.py` (add `get_call_stream_service`)
- Modify: `apps/control_plane/src/control_plane/main.py` (wire `app.state.call_stream_service` with a dedicated Redis client, like the transcript service at lines 106-119; close its client in teardown; accept an optional `call_stream_service=` factory kwarg mirroring `transcript_service=` for tests)
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py` (the endpoint)
- Test: `tests/integration/control_plane/test_calls.py` (extend)

**Interfaces:**
- Consumes: `CallStreamService.consume` (Task 9); auth/streaming pattern copied from `voice_lab.stream_transcript` (`voice_lab.py:216-274`).
- Produces: `GET /calls/{call_id}/events` → `text/event-stream` of `id: <entry>\ndata: <CallStreamEvent JSON>\n\n` frames.

- [ ] **Step 1: Write the failing tests** — extend `tests/integration/control_plane/test_calls.py`, following its existing client/seed conventions and injecting an `InMemory`-style call-stream service via the new `create_app(call_stream_service=...)` kwarg (build a tiny in-memory store in the test copying the `_MemStore` from Task 9's tests, pre-loaded with one transcript event and then `mark_ended`):

```python
async def test_call_events_streams_envelope_frames_for_owner(...):
    # seed a Call owned by the caller; preload the fake stream with one
    # transcript envelope + ended sentinel; GET /calls/{id}/events
    # → 200, content-type text/event-stream, body contains '"type":"transcript"'

async def test_call_events_hidden_for_private_call_non_owner(...):
    # unpublished call, different owner → 404 (same shape as join-token)

async def test_call_events_unknown_call_returns_404(...):
```

- [ ] **Step 2: Run to verify failure**

Run: `just test tests/integration/control_plane/test_calls.py -v -k call_events`
Expected: FAIL — 404 route not found / TypeError on create_app kwarg

- [ ] **Step 3: Wire state + dep**

`deps.py` (next to `get_transcript_service`):

```python
def get_call_stream_service(request: Request) -> CallStreamService:
    service: CallStreamService = request.app.state.call_stream_service
    return service
```

(import `CallStreamService` from `vera_core.call_stream`.)

`main.py`: add a `call_stream_service: CallStreamService | None = None` kwarg to `create_app`; inside the lifespan, right after the transcript-service block:

```python
        _call_stream_service = call_stream_service
        call_stream_redis: Redis | None = None
        if _call_stream_service is None:
            # Dedicated client: a tailing SSE pins a connection (same reason as the
            # transcript stream's own client).
            call_stream_redis = create_redis(settings.redis_url)
            _call_stream_service = CallStreamService(
                RedisCallStreamStore(
                    call_stream_redis,
                    ttl_seconds=settings.transcript_stream_ttl_seconds,
                    end_grace_seconds=settings.transcript_end_grace_seconds,
                )
            )
        app.state.call_stream_service = _call_stream_service
```

and in the shutdown section: `if call_stream_redis is not None: await call_stream_redis.aclose()`.

- [ ] **Step 4: Add the endpoint to `calls.py`**

Imports to add: `Depends`, `StreamingResponse` (from `fastapi.responses`), `AsyncIterator` (collections.abc), `Annotated`, `async_sessionmaker`/`AsyncSession`, `current_identity`, `get_sessionmaker`, `get_call_stream_service`, `PermissionResolver`/`get_resolver` (from `control_plane.auth.rbac`), `tenant_session` (from `vera_core.db.rls`), `CallStreamService` (from `vera_core.call_stream`).

```python
@router.get("/calls/{call_id}/events")
async def stream_call_events(
    call_id: UUID,
    request: Request,
    identity: Annotated[VerifiedIdentity, Depends(current_identity)],
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    resolver: Annotated[PermissionResolver, Depends(get_resolver)],
    audit: Audit,
    service: Annotated[CallStreamService, Depends(get_call_stream_service)],
) -> StreamingResponse:
    """Live per-call event stream (transcript turns, call_status frames; form-fill
    later) for Live Monitoring. Same visibility rule as join-token: owner, or a
    published/ownerless call, minus revoked users. Authorization runs in a
    SHORT-LIVED tenant session released before streaming (an SSE is long-lived and
    must not pin a DB connection — mirrors voice_lab.stream_transcript)."""
    if identity.account_type != "tenant" or identity.tenant_id is None:
        raise NotFoundError(message="call not found")
    tenant_id = identity.tenant_id
    async with tenant_session(sessionmaker, tenant_id) as session:
        user_id, permissions = await resolver.effective_permissions(
            session, tenant_id, identity.user_id
        )
        call = (
            await session.execute(select(Call).where(Call.id == call_id))
        ).scalar_one_or_none()
    if call is None:
        raise NotFoundError(message="call not found")
    if call.initiated_by_id != user_id:
        revoked = str(user_id) in call.revoked_user_ids
        if revoked or (call.initiated_by_id is not None and not call.published):
            raise NotFoundError(message="call not found")  # don't reveal a private call
    allowed = "calls:read" in permissions
    # Transcript text is tokenized/de-identified, but the disclosure is still audited
    # (mirrors the voice-lab transcript endpoint).
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=user_id,
            actor_label=identity.email or identity.subject,
            event_type=AuditEvent.PHI_ACCESS.value,
            resource_type="call_events",
            resource_id=str(call_id),
            permission_key="calls:read",
            decision="allow" if allowed else "deny",
            request_id=current_request_id(request),
        )
    )
    if not allowed:
        raise CustomAPIException(
            DefaultExceptionCode.FORBIDDEN, message="missing permission calls:read"
        )
    room_name = room_name_for_call(tenant_id, call.id)

    async def _events() -> AsyncIterator[str]:
        async for entry_id, event in service.consume(room_name):
            yield f"id: {entry_id}\ndata: {event.model_dump_json()}\n\n"

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 5: Run tests + gate**

Run: `just test tests/integration/control_plane/test_calls.py -v -k call_events` → PASS
Run: `just check` → PASS

- [ ] **Step 6: Commit**

```bash
git add -A vera-backend
git commit -m "feat(api): GET /calls/{id}/events — live envelope SSE for real calls"
```

---

### Task 11: Remove the manual start-call endpoint and the HTTP status callback

The queue dispatcher is now the only production initiator and the consumer the only terminal writer — delete `POST /calls`, `POST /calls/{id}/status`, `StartCallRequest`, and the frontend's dead `startCall`.

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py` (delete `start_call` at lines 81-177 and `update_call_status` + `UpdateCallStatusRequest` + `_TERMINAL_FAILURE_STATUSES`/`_ALLOWED_CALLBACK_*` at lines 404-536; prune now-unused imports: `contextlib`, `InsuranceProvider`, `ProviderStatus`, `FormStatus`, `FormStateMachine`, `InvalidTransitionError`, `try_dispatch`, `add_active_playbook_metadata`, `PersonaTweak`, `StartCallRequest`, `Tenant`, `get_audit` — keep only what the surviving endpoints use)
- Modify: `packages/vera_core/src/vera_core/schemas/dto.py:24-31` (delete `StartCallRequest`) and its export in `packages/vera_core/src/vera_core/schemas/__init__.py`
- Modify: `vera-frontend/src/lib/api/calls.ts:36-39` (delete `startCall`)
- Test: `tests/integration/control_plane/test_calls.py`, `tests/integration/control_plane/test_call_queue.py`, `tests/unit/schemas/test_call_dtos.py`

**Interfaces:**
- Surviving `/calls` surface: `GET /calls`, `GET /calls/{id}/join-token`, `GET /calls/{id}/events`, `POST /calls/{id}/publish`, `POST /calls/{id}/revoke-access`.

- [ ] **Step 1: Add a direct-DB call factory for tests**

Many `test_calls.py` tests used `POST /calls` as setup. Add to `tests/integration/control_plane/conftest.py`:

```python
async def seed_call(
    sessionmaker,
    tenant_id,
    form_id,
    *,
    initiated_by_id=None,
    status="initiated",
    published=False,
):
    """Insert a Call row directly — the manual start-call endpoint is gone; tests
    seed call state the way the dispatcher would."""
    from vera_core.db.rls import tenant_session
    from vera_core.models import Call

    async with tenant_session(sessionmaker, tenant_id) as session:
        call = Call(
            tenant_id=tenant_id,
            form_id=form_id,
            current_status=status,
            initiated_by_id=initiated_by_id,
            published=published,
        )
        session.add(call)
        await session.flush()
        return call.id
```

- [ ] **Step 2: Rework the tests**

- Delete outright (they test the removed endpoints): `test_create_call_unknown_form_returns_404`, `test_create_call_unknown_provider_returns_404`, `test_create_call_inactive_provider_returns_404`, `test_create_call_nests_persona_tweak_in_dispatch_metadata`, `test_create_call_summary_reports_owner_and_private` in `test_calls.py`; `test_manual_call_then_completed_callback`, `test_manual_call_form_not_redispatched`, `test_completed_callback_moves_form_to_completed` in `test_call_queue.py` (the completed-path coverage now lives in Task 7's consumer tests).
- Convert the rest of `test_calls.py` that used `POST /calls` for setup (`test_list_calls_empty_then_populated`, `test_join_token_returns_room_scoped_token`, `test_new_call_is_private_by_default`, `test_list_scopes_to_owner_or_published`, `test_ownerless_call_is_tenant_visible_and_joinable`, `test_publish_is_owner_only_idempotent_and_audited`, `test_join_token_gated_and_audited_for_non_owner`, `test_owner_revokes_intervener_access`, `test_owner_revoke_of_departed_intervener_is_noop_but_audited`, `test_supervisor_token_can_list_calls`, `test_list_calls_sets_no_store_and_audits_phi_disclosure`) to seed via `seed_call(...)` — assertions unchanged.
- `tests/unit/schemas/test_call_dtos.py`: drop the `StartCallRequest` cases, keep the rest.
- In `test_calls.py::test_calls_require_auth`, remove the `POST /calls` and `POST /calls/{id}/status` entries from its route list; add `GET /calls/{id}/events`.

- [ ] **Step 3: Delete the endpoints + schema + frontend function** (as listed under Files above).

- [ ] **Step 4: Run the full backend gate + frontend gate**

Run: `just check` → PASS
Run: `cd vera-frontend && npm run build && npm run lint && npm test` → PASS (`startCall` had no call sites; `tsc -b` proves it)

- [ ] **Step 5: Commit**

```bash
git add -A vera-backend vera-frontend
git commit -m "refactor(calls): remove manual start-call endpoint and HTTP status callback"
```

---

### Task 12: Frontend — live transcript in the monitoring modals + IVR toggle on queue

Stream `GET /calls/{id}/events` and render transcript turns in the Overview modal's "Live Transcripts" panel and the Intervene modal's transcript tab (audio join / LiveCallRoom stays as-is). Also surface the voice-lab-style **IVR navigation toggle** next to the form modal's "queue" action so the operator chooses per call (Task 2's `enable_ivr_navigation` field).

**Files:**
- Create: `vera-frontend/src/lib/api/callEvents.ts`
- Create: `vera-frontend/src/components/monitoring/CallTranscript.tsx`
- Modify: `vera-frontend/src/components/monitoring/CallOverviewModal.tsx:192-209` (right panel)
- Modify: `vera-frontend/src/components/monitoring/InterveneModal.tsx` (transcript tab content)
- Modify: `vera-frontend/src/lib/patient-forms/api.ts:55-63` (`updatePatientFormStatus` options)
- Modify: `vera-frontend/src/components/ibv/IbvProvider.tsx:270-293` (`changeStatus` passthrough)
- Modify: `vera-frontend/src/components/ibv/IbvFormModal.tsx:80-94` (toggle beside the queue button)
- Test: `vera-frontend/src/lib/api/callEvents.test.ts`

**Interfaces:**
- Consumes: SSE frames of `CallStreamEvent` JSON (Task 10): `{type: string, data: object, ts: number}`; transcript frames carry `data: {role: "user"|"agent", text: string}`; `PUT /patient-forms/{id}/status` body `{status, enable_ivr_navigation?}` (Task 2).
- Produces: `streamCallEvents(callId, {signal, onEvent})`, `asTranscriptTurn(e): TranscriptTurn | null`, `<CallTranscript callId={string} />`; `updatePatientFormStatus(formId, status, opts?: { enableIvrNavigation?: boolean })`; IbvProvider context gains `ivrNavigation: boolean` / `setIvrNavigation(v: boolean)` (pre-loaded from the form detail; `changeStatus(next)` keeps its signature and sends the value itself on an `in_queue` change).

- [ ] **Step 1: Write the failing test** — `src/lib/api/callEvents.test.ts` (mirror the project's vitest conventions; if `transcription.ts` has a test, copy its fetch-mocking approach):

```ts
import { describe, expect, it } from "vitest"

import { asTranscriptTurn, type CallStreamEvent } from "@/lib/api/callEvents"

describe("asTranscriptTurn", () => {
  it("maps a transcript envelope to a turn", () => {
    const e: CallStreamEvent = {
      type: "transcript",
      data: { role: "agent", text: "hello" },
      ts: 42,
    }
    expect(asTranscriptTurn(e)).toEqual({ role: "agent", text: "hello", ts: 42 })
  })

  it("ignores non-transcript envelopes", () => {
    expect(asTranscriptTurn({ type: "call_status", data: { status: "active" }, ts: 1 })).toBeNull()
  })

  it("ignores malformed transcript data", () => {
    expect(asTranscriptTurn({ type: "transcript", data: { role: "narrator" }, ts: 1 })).toBeNull()
  })
})
```

- [ ] **Step 2: Run to verify failure**

Run: `cd vera-frontend && npx vitest run src/lib/api/callEvents.test.ts`
Expected: FAIL — module not found

- [ ] **Step 3: Create `src/lib/api/callEvents.ts`**

```ts
// Live call-event SSE client (real-call flow). Envelope stream: transcript turns,
// call_status frames, and future event types (form-fill) ride one connection.
// fetch + ReadableStream (not EventSource) so the Authorization header can be sent;
// reconnect = re-call (the endpoint replays from the start). Mirrors transcription.ts.

import { ApiError, BASE_URL } from "@/lib/api/client"
import { getToken } from "@/lib/auth/storage"

export type CallStreamEvent = { type: string; data: Record<string, unknown>; ts: number }
export type TranscriptTurn = { role: "user" | "agent"; text: string; ts: number }

/** Narrow an envelope to a transcript turn; null for other/malformed event types. */
export function asTranscriptTurn(e: CallStreamEvent): TranscriptTurn | null {
  if (e.type !== "transcript") return null
  const { role, text } = e.data as { role?: unknown; text?: unknown }
  if ((role !== "user" && role !== "agent") || typeof text !== "string") return null
  return { role, text, ts: e.ts }
}

export async function streamCallEvents(
  callId: string,
  opts: { signal: AbortSignal; onEvent: (e: CallStreamEvent) => void },
): Promise<void> {
  const res = await fetch(`${BASE_URL}/calls/${encodeURIComponent(callId)}/events`, {
    method: "GET",
    headers: { Authorization: `Bearer ${getToken()}`, Accept: "text/event-stream" },
    signal: opts.signal,
  })
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, null, `call event stream failed (${res.status})`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const frames = buffer.split("\n\n")
    buffer = frames.pop() ?? ""
    for (const frame of frames) {
      const dataLine = frame.split("\n").find((l) => l.startsWith("data:"))
      if (!dataLine) continue
      const json = dataLine.slice(5).trim()
      if (json) opts.onEvent(JSON.parse(json) as CallStreamEvent)
    }
  }
}
```

- [ ] **Step 4: Run the unit test**

Run: `npx vitest run src/lib/api/callEvents.test.ts`
Expected: PASS

- [ ] **Step 5: Create `src/components/monitoring/CallTranscript.tsx`**

```tsx
import { useEffect, useRef, useState } from "react"
import { MessageSquare } from "lucide-react"

import { cn } from "@/lib/utils"
import {
  asTranscriptTurn,
  streamCallEvents,
  type TranscriptTurn,
} from "@/lib/api/callEvents"

/**
 * Live transcript feed for a call, from the /calls/{id}/events SSE.
 * PHI hygiene: turns are tokenized server-side and held in component state only —
 * discarded on unmount (closing the modal). Never persisted or logged.
 */
export function CallTranscript({ callId }: { callId: string }) {
  const [turns, setTurns] = useState<TranscriptTurn[]>([])
  const [error, setError] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const controller = new AbortController()
    setTurns([])
    setError(null)
    streamCallEvents(callId, {
      signal: controller.signal,
      onEvent: (e) => {
        const turn = asTranscriptTurn(e)
        if (turn) setTurns((prev) => [...prev, turn])
      },
    }).catch((err) => {
      if (!controller.signal.aborted)
        setError(err instanceof Error ? err.message : "Transcript unavailable.")
    })
    return () => controller.abort()
  }, [callId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [turns.length])

  if (error) {
    return <p className="p-4 text-sm text-muted-foreground">{error}</p>
  }
  if (turns.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 py-10 text-muted-foreground">
        <MessageSquare className="size-8 opacity-30" />
        <span className="text-sm">Waiting for transcript…</span>
      </div>
    )
  }
  return (
    <div className="flex-1 space-y-2 overflow-y-auto p-4">
      {turns.map((t, i) => (
        <div
          key={`${t.ts}-${i}`}
          className={cn("flex", t.role === "agent" ? "justify-start" : "justify-end")}
        >
          <div
            className={cn(
              "max-w-[85%] rounded-lg px-3 py-2 text-sm",
              t.role === "agent"
                ? "bg-muted text-foreground"
                : "bg-primary/10 text-foreground",
            )}
          >
            <span className="mb-0.5 block text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              {t.role === "agent" ? "Vera" : "Rep"}
            </span>
            {t.text}
          </div>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
```

- [ ] **Step 6: Wire into `CallOverviewModal.tsx`**

Replace the right-panel body (lines 201-208, the `call?.id ? <LiveCallRoom ... /> : ...` block) with a stacked audio-join + transcript:

```tsx
            {call?.id ? (
              <div className="flex min-h-0 flex-1 flex-col">
                <div className="shrink-0 border-b border-border">
                  <LiveCallRoom key={call.id} callId={call.id} />
                </div>
                <CallTranscript key={`t-${call.id}`} callId={call.id} />
              </div>
            ) : (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 py-16 text-muted-foreground">
                <MessageSquare className="size-10 opacity-30" />
                <span className="text-sm">No call selected</span>
              </div>
            )}
```

Add the import: `import { CallTranscript } from "./CallTranscript"`.

- [ ] **Step 7: Wire into `InterveneModal.tsx`**

The modal already has a `tab: "info" | "transcript"` state. Locate the `tab === "transcript"` branch in its body (search for `"transcript"` in the JSX) and render `<CallTranscript callId={call.id} />` there when `call?.id` is set (same no-call fallback as Step 6). Add the same import.

- [ ] **Step 8: IVR toggle on the queue action**

`src/lib/patient-forms/api.ts` — extend the status updater (keep the existing doc comment style):

```ts
/** PUT /patient-forms/{id}/status — change lifecycle status (status only).
 *  `enableIvrNavigation` rides only with an in_queue change (voice-lab-style
 *  toggle); omitted → the backend keeps the form's stored choice. */
export function updatePatientFormStatus(
  formId: string,
  status: PatientFormStatus,
  opts?: { enableIvrNavigation?: boolean },
): Promise<PatientFormStatusAck> {
  return apiRequest<PatientFormStatusAck>(
    `/patient-forms/${encodeURIComponent(formId)}/status`,
    {
      method: "PUT",
      body: {
        status,
        ...(opts?.enableIvrNavigation !== undefined
          ? { enable_ivr_navigation: opts.enableIvrNavigation }
          : {}),
      },
    },
  )
}
```

(Adapt the return-type name to the file's existing ack type.)

`src/lib/patient-forms/types.ts` — add to `PatientFormDetail` (Task 2 exposes it):

```ts
  /** Stored queue-time choice: run the IVR navigator on this form's calls. */
  ivr_navigation_enabled: boolean
```

`src/components/ibv/IbvProvider.tsx` — the toggle lives in the provider, pre-loaded from the fetched detail so a requeue shows the stored choice (initial/reset value `true` — matches the backend default; navigation is the normal state):

```ts
  const [ivrNavigation, setIvrNavigation] = useState(true)
```

next to the other status state (line ~141); reset `setIvrNavigation(true)` alongside the existing `setStatus(null)` resets (lines ~170 and ~193); on detail load, next to `setStatus(detail.status)` (line ~205):

```ts
          setIvrNavigation(detail.ivr_navigation_enabled)
```

and in `changeStatus` (signature unchanged), send it with a queue change:

```ts
        const res = await updatePatientFormStatus(
          formId,
          next,
          next === "in_queue" ? { enableIvrNavigation: ivrNavigation } : undefined,
        )
```

Expose `ivrNavigation` and `setIvrNavigation` on the context value + type.

`src/components/ibv/IbvFormModal.tsx` — a Switch beside the transition buttons, bound to the provider state (destructure `ivrNavigation, setIvrNavigation` from `useIbv()`); inside the `{canWrite && transitions.length > 0 && (...)}` block, before the buttons `map`:

```tsx
                {transitions.includes("in_queue") && (
                  <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <Switch
                      checked={ivrNavigation}
                      onCheckedChange={setIvrNavigation}
                      disabled={statusChanging}
                    />
                    IVR navigation
                  </label>
                )}
```

The buttons' `onClick={() => changeStatus(target)}` stays unchanged — the provider attaches the toggle itself. (Import `Switch` from `@/components/ui/switch` — same component Live Monitoring already uses.)

- [ ] **Step 9: Frontend gate**

Run: `cd vera-frontend && npm run build && npm run lint && npm test`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add -A vera-frontend
git commit -m "feat(frontend): live transcript panel + IVR navigation toggle on queue"
```

---

### Task 13: Pipeline sweeper — stuck-call reconciliation + time-based dispatch wake-up

One background loop (lifespan task, like the worker-event consumer) closes the two timing holes in the event-driven design: a crashed worker never emits `call.ended` (form stuck IN_CALL, slot leaked forever), and nothing re-triggers dispatch when working hours reopen / queue expiry passes. Per sweep, per tenant: fail any non-terminal call whose LiveKit room is gone (grace-protected) or that exceeds the hard duration cap — through the same `close_call` path as the consumer — then run a dispatch pass when there is queued work. Tenant ids come from `platform_session` (tenant catalog is platform-readable — migration `0022`); all row work runs under `tenant_session` (RLS intact). Safe with multiple control-plane replicas: `close_call` re-checks under a row lock and is idempotent.

**Files:**
- Modify: `apps/control_plane/src/control_plane/livekit_gateway.py` (add `existing_rooms`)
- Modify: `packages/vera_core/src/vera_core/config/settings.py` (three settings)
- Create: `apps/control_plane/src/control_plane/pipeline_sweeper.py`
- Modify: `apps/control_plane/src/control_plane/main.py` (lifespan wiring + teardown)
- Test: `tests/unit/control_plane/test_pipeline_sweeper.py`

**Interfaces:**
- Consumes: `close_call` / `TERMINAL_VALUES` (Task 7), `run_dispatch_pass` (Task 5), `platform_session` / `tenant_session` (`vera_core.db.rls`), `room_name_for_call`.
- Produces: `LiveKitGateway.existing_rooms(room_names: list[str]) -> set[str]`; `PipelineSweeper(sessionmaker, livekit, kms, audit, *, interval_s, stuck_grace_s, max_call_duration_s)` with `run()` (loop) and `sweep_once()`; pure helper `rooms_to_close(rows, live_rooms, tenant_id) -> list[tuple[str, bool]]`; settings `pipeline_sweep_interval_seconds=60`, `call_stuck_grace_seconds=300`, `call_max_duration_seconds=10800`.

- [ ] **Step 1: Write the failing tests** — `tests/unit/control_plane/test_pipeline_sweeper.py`

```python
"""Pipeline sweeper — which stuck calls get closed, and when dispatch re-runs."""

from uuid import uuid4

import pytest

import control_plane.pipeline_sweeper as sweeper_mod
from control_plane.pipeline_sweeper import PipelineSweeper, rooms_to_close
from vera_core.observability.correlation import room_name_for_call


def test_room_gone_is_closed_without_room_delete():
    tenant, call = uuid4(), uuid4()
    room = room_name_for_call(tenant, call)
    result = rooms_to_close([(call, False)], live_rooms=set(), tenant_id=tenant)
    assert result == [(room, False)]  # close it; no room left to delete


def test_live_room_within_cap_is_left_alone():
    tenant, call = uuid4(), uuid4()
    room = room_name_for_call(tenant, call)
    assert rooms_to_close([(call, False)], live_rooms={room}, tenant_id=tenant) == []


def test_live_room_past_cap_is_deleted_then_closed():
    tenant, call = uuid4(), uuid4()
    room = room_name_for_call(tenant, call)
    result = rooms_to_close([(call, True)], live_rooms={room}, tenant_id=tenant)
    assert result == [(room, True)]  # wedged session: delete the room, then close


@pytest.mark.asyncio
async def test_sweep_once_continues_past_a_failing_tenant(monkeypatch):
    """One tenant's sweep error must not starve the others (loop isolation)."""
    tenants = [uuid4(), uuid4()]
    swept: list = []

    class _PlatformCtx:
        async def __aenter__(self):
            class _S:
                async def execute(self, *_a):
                    class _R:
                        def scalars(self):
                            class _V:
                                def all(self):
                                    return tenants

                            return _V()

                    return _R()

            return _S()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(sweeper_mod, "platform_session", lambda sm: _PlatformCtx())
    sweeper = PipelineSweeper(
        object(), object(), object(), object(),
        interval_s=60, stuck_grace_s=300, max_call_duration_s=10_800,
    )

    async def fake_sweep_tenant(tenant_id):
        swept.append(tenant_id)
        if tenant_id == tenants[0]:
            raise RuntimeError("boom")

    monkeypatch.setattr(sweeper, "_sweep_tenant", fake_sweep_tenant)
    await sweeper.sweep_once()  # must not raise
    assert swept == tenants
```

- [ ] **Step 2: Run to verify failure**

Run: `just test tests/unit/control_plane/test_pipeline_sweeper.py -v`
Expected: FAIL — `ModuleNotFoundError: control_plane.pipeline_sweeper`

- [ ] **Step 3: Add the gateway probe** — in `apps/control_plane/src/control_plane/livekit_gateway.py` (next to `delete_room`):

```python
    async def existing_rooms(self, room_names: list[str]) -> set[str]:
        """The subset of *room_names* that currently exist on the LiveKit server.
        One RPC; the pipeline sweeper uses "room gone but call non-terminal" as the
        worker-died signal (the healthy end path always deletes the room)."""
        if not room_names:
            return set()
        async with self._client() as lk:
            resp = await lk.room.list_rooms(api.ListRoomsRequest(names=room_names))
        return {room.name for room in resp.rooms}
```

- [ ] **Step 4: Add the settings** — in `packages/vera_core/src/vera_core/config/settings.py`, next to the worker-event settings:

```python
    # Pipeline sweeper: reconciles stuck calls (worker crash / lost event) and
    # wakes the dispatcher on a timer (working-hours reopen, queue expiry).
    pipeline_sweep_interval_seconds: int = 60  # VERA_PIPELINE_SWEEP_INTERVAL_SECONDS
    # A non-terminal call younger than the grace window is never touched — protects
    # the create→dial gap and normal-end races with the consumer.
    call_stuck_grace_seconds: int = 300  # VERA_CALL_STUCK_GRACE_SECONDS
    # Hard cap: a non-terminal call older than this gets its room deleted and is
    # failed even if the room is still alive (wedged worker session). Payer calls
    # with long holds run long — keep this generous.
    call_max_duration_seconds: int = 3 * 3600  # VERA_CALL_MAX_DURATION_SECONDS
```

- [ ] **Step 5: Create `apps/control_plane/src/control_plane/pipeline_sweeper.py`**

```python
"""Pipeline sweeper — the time-based safety net for the call pipeline.

The pipeline is event-driven (enqueue → dispatch; worker events → closeout →
refill), which leaves two timing holes this loop closes on every tick:

1. RECONCILE stuck calls. A hard-crashed worker never emits `call.ended`, so its
   form would sit IN_CALL forever and leak a concurrency slot. Signal: the healthy
   end path always deletes the LiveKit room (delete_room_on_close / the consumer's
   call.failed teardown), so a non-terminal Call whose room is GONE — past a grace
   window — is dead; it is failed through the same `close_call` path the consumer
   uses (bounded auto-retry, audit). A non-terminal call past the hard duration cap
   gets its room deleted first (ends a wedged session), then the same closeout.
2. WAKE the dispatcher. Queued forms whose blocking condition lapsed (working
   hours reopened, a slot freed by reconciliation) get a dispatch pass without
   waiting for the next enqueue/call-end event; queue expiry rides the same pass
   (try_dispatch expires stale forms).

Tenant enumeration runs under platform_session (the tenant catalog is
platform-readable — migration 0022); every row mutation runs per-tenant under
tenant_session, so RLS isolation is never bypassed. Concurrent sweepers (multiple
control-plane replicas) are safe: close_call re-checks terminal state under a row
lock, so the loser of a race is a no-op. No PHI flows here — ids, room names,
statuses, and counts only.
"""

import asyncio
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.call_closeout import TERMINAL_VALUES, close_call
from control_plane.dispatch import run_dispatch_pass
from vera_core.audit import AuditSink
from vera_core.db.rls import platform_session, tenant_session
from vera_core.models import Call, PatientForm, Tenant
from vera_core.models.enums import CallStatus, FormStatus
from vera_core.observability.correlation import room_name_for_call

logger = logging.getLogger("control_plane.pipeline_sweeper")


def rooms_to_close(
    rows: list[tuple[UUID, bool]], live_rooms: set[str], tenant_id: UUID
) -> list[tuple[str, bool]]:
    """Which stuck-call candidates to close: `(room_name, delete_room_first)`.

    rows: (call_id, past_cap) for non-terminal calls past the grace window.
    Room gone → close (the room needs no delete). Room live but past the hard
    cap → delete the room (ends the wedged session), then close. Room live and
    within the cap → a long call still in progress; leave it alone.
    """
    result: list[tuple[str, bool]] = []
    for call_id, past_cap in rows:
        room_name = room_name_for_call(tenant_id, call_id)
        if room_name not in live_rooms:
            result.append((room_name, False))
        elif past_cap:
            result.append((room_name, True))
    return result


class PipelineSweeper:
    """Periodic reconcile-and-dispatch loop; one per control-plane process."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        livekit: Any,
        kms: Any,
        audit: AuditSink,
        *,
        interval_s: float,
        stuck_grace_s: int,
        max_call_duration_s: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._livekit = livekit
        self._kms = kms
        self._audit = audit
        self._interval_s = interval_s
        self._stuck_grace_s = stuck_grace_s
        self._max_call_duration_s = max_call_duration_s

    async def run(self) -> None:
        """Sweep immediately on boot, then every interval. Mirrors the worker-event
        consumer's resilience: any error logs and waits for the next tick."""
        while True:
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pipeline sweep failed; retrying next interval")
            await asyncio.sleep(self._interval_s)

    async def sweep_once(self) -> None:
        async with platform_session(self._sessionmaker) as session:
            tenant_ids = list((await session.execute(select(Tenant.id))).scalars().all())
        for tenant_id in tenant_ids:
            try:
                await self._sweep_tenant(tenant_id)
            except Exception:  # one tenant's failure must not starve the rest
                logger.exception("sweep failed for tenant %s; continuing", tenant_id)

    async def _sweep_tenant(self, tenant_id: UUID) -> None:
        # Phase 1 (read-only, lock-free, DB-clock interval math): stuck-call
        # candidates + whether any dispatchable work is queued.
        # func.make_interval args are positional (years, months, weeks, days,
        # hours, mins, secs) — seconds is the 7th.
        grace = func.make_interval(0, 0, 0, 0, 0, 0, self._stuck_grace_s)
        cap = func.make_interval(0, 0, 0, 0, 0, 0, self._max_call_duration_s)
        async with tenant_session(self._sessionmaker, tenant_id) as session:
            rows = [
                (row.id, row.past_cap)
                for row in (
                    await session.execute(
                        select(
                            Call.id,
                            (Call.created_at < func.now() - cap).label("past_cap"),
                        ).where(
                            Call.tenant_id == tenant_id,
                            Call.current_status.not_in(list(TERMINAL_VALUES)),
                            Call.created_at < func.now() - grace,
                        )
                    )
                ).all()
            ]
            has_queued = (
                await session.execute(
                    select(PatientForm.id)
                    .where(PatientForm.status == FormStatus.IN_QUEUE.value)
                    .limit(1)
                )
            ).scalar_one_or_none() is not None

        # Phase 2: probe LiveKit once, close the dead ones via the shared path.
        closed = 0
        if rows:
            candidate_rooms = [room_name_for_call(tenant_id, cid) for cid, _ in rows]
            live_rooms = await self._livekit.existing_rooms(candidate_rooms)
            for room_name, delete_first in rooms_to_close(rows, live_rooms, tenant_id):
                if delete_first:
                    logger.warning(
                        "sweeper: room %s past max call duration; deleting", room_name
                    )
                    await self._livekit.delete_room(room_name)
                ref = await close_call(
                    self._sessionmaker,
                    self._audit,
                    room_name,
                    CallStatus.FAILED,
                    trigger="sweeper_reconcile",
                    actor_label="pipeline-sweeper",
                )
                if ref is not None:
                    closed += 1
                    logger.info("sweeper: reconciled stuck call room %s", room_name)

        # Phase 3: time-based dispatch wake-up — freed slots and/or queued forms.
        if closed or has_queued:
            await run_dispatch_pass(
                self._sessionmaker, tenant_id, self._livekit, self._kms, self._audit
            )
```

- [ ] **Step 6: Run the unit tests**

Run: `just test tests/unit/control_plane/test_pipeline_sweeper.py -v`
Expected: PASS

- [ ] **Step 7: Wire into the lifespan** — in `apps/control_plane/src/control_plane/main.py`, right after the worker-event consumer block (same `settings.livekit_url is not None and app.state.livekit is not None` guard — add to that existing `if` body):

```python
            sweeper = PipelineSweeper(
                sessionmaker,
                app.state.livekit,
                app.state.kms,
                app.state.audit,
                interval_s=settings.pipeline_sweep_interval_seconds,
                stuck_grace_s=settings.call_stuck_grace_seconds,
                max_call_duration_s=settings.call_max_duration_seconds,
            )
            sweeper_task = asyncio.create_task(sweeper.run())
            sweeper_task.add_done_callback(_log_consumer_exit)
```

with `sweeper_task: asyncio.Task[None] | None = None` initialized beside `worker_event_task`, import `from control_plane.pipeline_sweeper import PipelineSweeper`, and in the shutdown section (next to the consumer's cancel):

```python
        if sweeper_task is not None:
            sweeper_task.cancel()
            with suppress(asyncio.CancelledError):
                await sweeper_task
```

(Reuse the existing `_log_consumer_exit` done-callback; if its log message hardcodes "consumer", generalize the message or add a sibling `_log_sweeper_exit` with the same shape.)

- [ ] **Step 8: Run the full gate**

Run: `just check`
Expected: PASS (if `tests/integration/control_plane/test_app_boot.py` asserts lifespan task behavior, extend it for the sweeper task the same way it covers the consumer)

- [ ] **Step 9: Commit**

```bash
git add -A vera-backend
git commit -m "feat(sweeper): reconcile stuck calls and wake the dispatcher on a timer"
```

---

### Task 14: Boot verification, simplification, final gates

Backend CLAUDE.md mandates booting for background-loop changes (the consumer gained DB writes; the worker gained emit paths), and the repo CLAUDE.md mandates a code-simplifier pass before "done".

- [ ] **Step 1: Boot the stack and idle-verify both background loops**

From `vera-backend/`:

```bash
just up && just migrate
LOCAL_KMS_MASTER_KEY=<dev key> VERA_LIVEKIT_URL=ws://localhost:7880 \
  VERA_PIPELINE_SWEEP_INTERVAL_SECONDS=10 just api
```

Watch ≥2 consumer poll windows and ≥2 sweep ticks (30+ s): no tracebacks, no `worker-event consumer Redis error` spam (idle `TimeoutError` must be silent), no `pipeline sweep failed` per-tick errors while idle.

Then verify sweeper reconciliation with a synthetic stuck call: insert a Call row (`current_status='active'`, `created_at` backdated > grace, form `in_call`) via psql or a scratch script, with no matching LiveKit room. Within one sweep tick: the call flips to `failed`, the form auto-requeues (`retry_count` +1), a `sweeper: reconciled stuck call room …` log line appears, and a dispatch pass runs.

- [ ] **Step 2: End-to-end smoke (no real telephony needed)**

In a second terminal: `just worker`. Then, with seeded data (`just test_seed_patient_data` seeds `ready_for_processing` forms):
1. `PUT /patient-forms/{id}/status {"status": "in_queue"}` **without** a seeded trunk → expect the 409 "outbound calling is not configured" from the gate.
2. Seed the trunk credential (Integrations settings UI or the seeding used in tests), re-enqueue → expect 200; the dispatcher will dial via LiveKit SIP; with no real trunk the dial raises `OutboundDialError` → verify the call row is `failed`, the form auto-requeued with `retry_count` incremented, and the room deleted (LiveKit dashboard / logs).
3. Verify `GET /calls` shows dispatcher-created calls and `GET /calls/{id}/events` streams (even if only the ended sentinel).

Record what was actually observed — this is the evidence for "done".

- [ ] **Step 3: Simplify**

Run the **code-simplifier** agent over the changed files (repo CLAUDE.md mandate), then re-run both gates:

```bash
just check
cd ../vera-frontend && npm run build && npm run lint && npm test
```

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: post-simplification cleanups for call pipeline integration"
```

---

## Known limitations (accepted, documented, not built)

1. **Worker still has no form context / writes no AI_CALL answers** — separate developer's workstream; this plan only guarantees the lifecycle loop they will plug into.

(Previously listed here and now IN scope via Task 13's pipeline sweeper: worker-crash → stuck IN_CALL reconciliation, and time-based dispatcher wake-up.)
