# Call Queue & Dispatch — Design Spec

**Date:** 2026-06-29
**Status:** Approved
**Scope:** Tenant queue submission gating, event-driven dispatch, form status state machine, max-retry cap, queue expiry

---

## 1. Problem

Vera currently initiates calls manually — a user clicks "start call" which synchronously creates a Call record and dispatches an agent. There is no queuing, no concurrency control, no automatic retry, and no awareness of insurance provider working hours. This spec introduces a queue-and-dispatch system that gates call initiation behind tenant concurrency limits and provider working hours, automatically retries failed calls, and expires stale queue entries.

## 2. Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Enqueue trigger | Manual (user clicks) | Forms need review before calling |
| Concurrency cap | `Tenant.max_agents_per_va` (existing) | Already exists, default 3 |
| Working hours source | `InsuranceProvider.working_hour_start/end` | Already exists; fixed US Eastern timezone |
| Dispatch model | Event-driven, in-process | Single replica, no new infra, pure async |
| Dispatch triggers | (1) form enqueued, (2) call ends | Covers both "new work" and "slot freed" |
| Queue ordering | FIFO by `enqueued_at` | Simplest, fair |
| Retry behavior | Automatic, up to `tenant.max_retries` | Failed forms re-enter queue back |
| Expiry check | Lazy (at dispatch time) | No sweeper cron needed |
| Concurrency safety | `SELECT ... FOR UPDATE SKIP LOCKED` | Prevents double-dispatch from concurrent triggers |

## 3. Data Model Changes

### 3.1 Tenant — two new columns

```python
max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
queue_expiry_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=48)
```

- `max_retries`: maximum call retry attempts per form before `CALL_FAILED` becomes terminal.
- `queue_expiry_hours`: hours after enqueue before a form transitions to `EXPIRED`.

### 3.2 PatientForm — one new column

```python
enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Set when a form transitions to `IN_QUEUE`. Drives FIFO ordering and expiry calculation. Reset on re-enqueue (retry goes to back of queue).

### 3.3 FormStatus enum — one new value

```python
EXPIRED = "expired"  # queue expiry reached without completion
```

Terminal state. The CHECK constraint migration must include this value.

### 3.4 No new tables

The queue is implicit: `PatientForm` rows with `status = 'in_queue'`. The existing partial index `ix_patient_form_queued` already covers this query.

## 4. State Machine

### 4.1 Allowed transitions

```
READY_FOR_PROCESSING  →  IN_QUEUE
READY_FOR_PROCESSING  →  EXCEPTION_REVIEW

IN_QUEUE              →  IN_CALL
IN_QUEUE              →  EXPIRED

IN_CALL               →  AI_PROCESSING
IN_CALL               →  CALL_FAILED

AI_PROCESSING         →  COMPLETED
AI_PROCESSING         →  CALL_FAILED

CALL_FAILED           →  IN_QUEUE          (auto-retry, guard: retry_count < max_retries)

EXCEPTION_REVIEW      →  IN_QUEUE
EXCEPTION_REVIEW      →  COMPLETED         (manual: once all disputes resolved)
```

Terminal states: `COMPLETED`, `CALL_FAILED` (retries exhausted), `EXPIRED`.

### 4.2 Transition side effects

| Transition | Side effect |
|------------|-------------|
| `* → IN_QUEUE` | Set `enqueued_at = now()` |
| `CALL_FAILED → IN_QUEUE` | Increment `retry_count`, create `CallLineage` |
| `IN_QUEUE → EXPIRED` | No side effect beyond status change |
| `IN_QUEUE → IN_CALL` | Create `Call` record, create LiveKit room, dispatch agent |

### 4.3 Enforcement

`FormStateMachine` service validates every transition against the allowed-transitions map. All form status changes go through this service — no direct `form.status = ...` assignment outside it.

## 5. Dispatcher

### 5.1 Entry point

```python
async def try_dispatch(session_factory, tenant_id, livekit) -> int:
    """Attempt to dispatch queued forms for a tenant. Returns count of calls initiated."""
```

Pure async function in `vera_core/services/queue_dispatcher.py`. No framework dependency — testable with a plain `AsyncSession`.

### 5.2 Algorithm

```
1. Load tenant config (max_agents_per_va, queue_expiry_hours)
2. Count active calls: status IN ('in_call', 'ai_processing')
3. slots_available = max_agents_per_va - active_count
4. If slots_available <= 0 → return 0
5. SELECT patient_form WHERE status = 'in_queue' AND tenant_id = ?
   ORDER BY enqueued_at ASC
   FOR UPDATE SKIP LOCKED
   LIMIT slots_available
6. For each candidate:
   a. Expiry check: if now() > enqueued_at + queue_expiry_hours → transition EXPIRED, continue
   b. Working hours check: resolve insurance provider → if outside hours → skip (leave IN_QUEUE)
   c. Transition IN_QUEUE → IN_CALL (via state machine)
   d. Create Call record (mode=FULL or RETRY based on retry_count)
   e. Create LiveKit room with tenant persona metadata
   f. Write CallEvent (INITIATED)
7. Commit, return count of dispatched calls
```

### 5.3 Working hours gate

```python
def is_within_working_hours(provider: InsuranceProvider) -> bool:
    if provider.working_hour_start is None or provider.working_hour_end is None:
        return True  # no hours configured = always available
    eastern = ZoneInfo("America/New_York")
    now_time = datetime.now(eastern).time()
    return provider.working_hour_start <= now_time <= provider.working_hour_end
```

Forms whose provider is outside working hours stay `IN_QUEUE` and are re-evaluated on the next dispatch trigger.

### 5.4 Concurrency safety

- `FOR UPDATE SKIP LOCKED` on form rows prevents double-dispatch from concurrent triggers.
- Active call count is checked inside the same transaction.
- Single control plane replica means no cross-process race.

## 6. Event Triggers

### 6.1 Form enqueued

**Where:** `PUT /patient-forms/{id}/status` endpoint (existing) in `control_plane/api/v1/patient_forms.py`.

**When:** Target status is `IN_QUEUE` and transition succeeds.

**Action:** After commit, call `try_dispatch(tenant_id)`.

### 6.2 Call ends

**Where:** New endpoint `POST /calls/{call_id}/status` (internal, called by agent worker callback).

**When:** Call reaches terminal status (`COMPLETED`, `FAILED`, `NO_ANSWER`, `BUSY`).

**Action:**
1. Update `call.current_status`, write `CallEvent`.
2. If terminal failure and `retry_count < max_retries`:
   - `FormStateMachine.transition(form, CALL_FAILED)`
   - `FormStateMachine.transition(form, IN_QUEUE)` (auto-retry, increments `retry_count`)
3. If terminal failure and retries exhausted:
   - `FormStateMachine.transition(form, CALL_FAILED)` (terminal)
4. If `COMPLETED`:
   - `FormStateMachine.transition(form, COMPLETED)`
5. Call `try_dispatch(tenant_id)` — a slot freed up.

## 7. Auto-Retry

- On call failure with retries remaining: `CALL_FAILED → IN_QUEUE` automatically.
- `retry_count` incremented, `enqueued_at` reset (retry goes to back of FIFO queue).
- `CallLineage` record created linking retry call to parent.
- New call created with `mode = CallMode.RETRY` so the agent can skip already-answered fields.
- Retry cap: `tenant.max_retries` (default 5, configurable per tenant).

## 8. Queue Expiry

- Checked lazily at dispatch time — no background sweeper.
- Condition: `now() > form.enqueued_at + timedelta(hours=tenant.queue_expiry_hours)`.
- Expired forms transition `IN_QUEUE → EXPIRED` (terminal).
- Default expiry window: 48 hours (configurable per tenant).
- Edge case: forms may appear as `IN_QUEUE` in the UI after logical expiry until the next dispatch pass. Acceptable for now; a lightweight periodic cleanup can be added later if needed.

## 9. New Modules

| Module | Package | Purpose |
|--------|---------|---------|
| `vera_core/services/form_state_machine.py` | `vera_core` | Transition validation, side effects, audit |
| `vera_core/services/queue_dispatcher.py` | `vera_core` | Dispatch engine (concurrency, FIFO, working hours, expiry) |

### Modified modules

| Module | Change |
|--------|--------|
| `vera_core/models/enums.py` | Add `EXPIRED` to `FormStatus` |
| `vera_core/models/tenant.py` | Add `max_retries`, `queue_expiry_hours` columns |
| `vera_core/models/patient_form.py` | Add `enqueued_at` column |
| `control_plane/api/v1/patient_forms.py` | Wire state machine + dispatch trigger on enqueue |
| `control_plane/api/v1/calls.py` | Add `POST /calls/{call_id}/status` for call-end callback; wire dispatch trigger |
| Alembic migration | New migration for schema changes + CHECK constraint update |

## 10. What Does NOT Change

- **Agent worker** — no changes. Dispatched to rooms by name as today.
- **PHI boundary** — dispatcher operates on form IDs and statuses only. No PHI flows through it.
- **RLS** — all queries run inside `tenant_session`. Existing policies apply automatically.
- **Existing `POST /calls`** — stays as manual override (for supervisor-initiated calls outside the queue).

## 11. Testing Strategy

- **Unit tests:** `FormStateMachine` transition validation (all valid/invalid pairs), side effects (enqueued_at, retry_count).
- **Unit tests:** `QueueDispatcher` — mock session with various queue states, verify correct forms dispatched in FIFO order, concurrency cap respected, working hours gating, expiry transitions.
- **Integration tests:** Full flow from enqueue → dispatch → call-end → auto-retry → dispatch, against real Postgres with RLS.
- **Edge cases:** concurrent dispatch triggers, all slots full, all forms expired, provider outside hours, retries exhausted, mixed providers (some in hours, some not).
