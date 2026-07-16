# Call-Cancel Lifecycle Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A supervisor's End Call always terminates the call promptly (no 5-minute zombie) and never triggers an automatic redial — with the sweeper, worker, and room lifecycle hardened so a call can only wedge if LiveKit, Redis, AND the sweeper are all broken at once.

**Architecture:** Add a `CANCELED` terminal `CallStatus` (user-intent, never auto-retried) plus a durable `call.end_requested_by_id` column stamped by the end-call endpoint. Pre-answer End Call closes the call synchronously through the shared `close_call` path (then deletes the room — order is load-bearing); post-answer End Call stamps intent and lets the worker's `call.ended` drive closeout, with the sweeper falling back to CANCELED (not FAILED) when the intent flag is set. Worker `wait_for_speaker` learns to resolve on room disconnect so it always publishes an outcome; rooms get explicit empty/departure timeouts; the sweeper additionally reaps live rooms held open only by browser observers.

**Tech Stack:** FastAPI control plane, SQLAlchemy + Alembic (Postgres), livekit-agents worker, Redis Streams worker events, React/Vite frontend.

## Global Constraints

- PHI rules per `vera-backend/CLAUDE.md`: no PHI in logs; ids/statuses only in audit detail.
- Migrations: revision ids are alembic's random hex (`just makemigration`); every incremental column add must be `ADD COLUMN IF NOT EXISTS`; constraints need the `DO $$ … duplicate_object` guard; `downgrade()` must be a real reverse. CI runs `alembic upgrade head` from `0001` on a **fresh** Postgres — `0001`'s `create_all` will already have built the new column/constraint/index from the live models.
- Backend gate: `just check` (ruff + mypy --strict + pytest) from `vera-backend/`. Frontend gate: `npm run lint && npm test && npm run build` from `vera-frontend/`.
- After the last task, run the code-simplifier agent on the change, then re-run both gates (repo rule).
- Existing constraint/index names on `call` (verified in DB): `ck_call_current_status_valid`, `uq_call_active_form` (partial unique on `form_id` WHERE status NOT IN terminal — **must be recreated with the widened terminal list or canceled calls block retries forever**).

---

### Task 1: CANCELED status + lifecycle semantics (vera_core)

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/enums.py` (CallStatus, ~line 33)
- Modify: `packages/vera_core/src/vera_core/models/call.py` (TERMINAL_CALL_STATUSES ~line 39; Call columns ~line 80)
- Modify: `packages/vera_core/src/vera_core/services/call_lifecycle.py` (_FORM_EDGE ~line 22; requeue branch ~line 45)
- Test: `tests/unit/services/test_call_lifecycle.py`

**Interfaces:**
- Produces: `CallStatus.CANCELED = "canceled"`; `Call.end_requested_by_id: UUID | None`; `TERMINAL_CALL_STATUSES` includes CANCELED; `apply_terminal_call_status(call, form, CallStatus.CANCELED, tenant_max_retries=N)` → form goes `CALL_FAILED`, returns `False` (never requeues).

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/services/test_call_lifecycle.py`, mirroring the fixtures already in that file):

```python
def test_canceled_is_terminal() -> None:
    assert CallStatus.CANCELED in TERMINAL_CALL_STATUSES


def test_canceled_parks_form_without_retry() -> None:
    call, form = _call(), _form(status=FormStatus.IN_CALL, retry_count=0)
    requeued = apply_terminal_call_status(
        call, form, CallStatus.CANCELED, tenant_max_retries=5
    )
    assert call.current_status == "canceled"
    assert form.status == FormStatus.CALL_FAILED.value
    assert requeued is False  # user intent: never auto-redial
```

(Use that file's existing `_call()`/`_form()` helpers; if they're named differently, follow the file's established fake constructors.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/services/test_call_lifecycle.py -q` → FAIL (`CANCELED` attribute missing).

- [ ] **Step 3: Implement**

`enums.py` — add to `CallStatus`:
```python
    CANCELED = "canceled"  # user-requested end (End Call in Live Monitoring); never auto-retried
```

`call.py`:
```python
TERMINAL_CALL_STATUSES = (
    CallStatus.COMPLETED,
    CallStatus.FAILED,
    CallStatus.NO_ANSWER,
    CallStatus.BUSY,
    CallStatus.CANCELED,
)
```
and on `Call`, next to `initiated_by_id`:
```python
    # Stamped by POST /calls/{id}/end BEFORE the room is torn down: durable "a user
    # asked to end this" signal. The sweeper closes such a call as CANCELED (no
    # auto-redial) if the worker's call.ended never arrives.
    end_requested_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
```

`call_lifecycle.py`:
```python
_FORM_EDGE: dict[CallStatus, FormStatus] = {
    CallStatus.COMPLETED: FormStatus.AI_PROCESSING,
    CallStatus.FAILED: FormStatus.CALL_FAILED,
    CallStatus.NO_ANSWER: FormStatus.CALL_FAILED,
    CallStatus.BUSY: FormStatus.CALL_FAILED,
    # User-requested end: park the form for a human; NEVER auto-requeue (a
    # supervisor who canceled a dial does not want the number redialed).
    CallStatus.CANCELED: FormStatus.CALL_FAILED,
}
```
and guard the retry branch:
```python
        if _FORM_EDGE[status] is FormStatus.CALL_FAILED and status is not CallStatus.CANCELED:
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/unit/services/test_call_lifecycle.py -q` → PASS.

- [ ] **Step 5: Commit** — `git add -A packages tests && git commit -m "feat(calls): CANCELED terminal status — user-requested end, never auto-retried"`

---

### Task 2: Migration — canceled status + end_requested_by_id

**Files:**
- Create: `migrations/versions/<generated>_call_canceled_status_and_end_requested_by.py` (via `just makemigration`, then replace the body)

**Interfaces:**
- Consumes: model changes from Task 1.
- Produces: DB accepts `current_status='canceled'`; `call.end_requested_by_id` exists; `uq_call_active_form` treats canceled as terminal.

- [ ] **Step 1: Generate the revision** — `just makemigration "call canceled status and end_requested_by"`. Delete every autogen op (it also emits unrelated index drops from known drift) and set `down_revision` to the current single head (`uv run alembic heads` → must print one).

- [ ] **Step 2: Write upgrade/downgrade** (all guards required — fresh CI DBs already have everything via `0001`'s `create_all`):

```python
_TERMINAL_OLD = "'completed', 'failed', 'no_answer', 'busy'"
_TERMINAL_NEW = "'completed', 'failed', 'no_answer', 'busy', 'canceled'"
_STATUSES_OLD = (
    "'initiated', 'ringing', 'ivr', 'active', 'waiting', 'critical', " + _TERMINAL_OLD
)
_STATUSES_NEW = (
    "'initiated', 'ringing', 'ivr', 'active', 'waiting', 'critical', " + _TERMINAL_NEW
)


def upgrade() -> None:
    op.execute("ALTER TABLE call ADD COLUMN IF NOT EXISTS end_requested_by_id UUID")
    op.execute(
        """
        DO $$ BEGIN
            ALTER TABLE call ADD CONSTRAINT fk_call_end_requested_by_id_app_user
                FOREIGN KEY (end_requested_by_id) REFERENCES app_user (id)
                ON DELETE SET NULL;
        EXCEPTION WHEN duplicate_object THEN NULL; END $$;
        """
    )
    op.execute("ALTER TABLE call DROP CONSTRAINT IF EXISTS ck_call_current_status_valid")
    op.execute(
        "ALTER TABLE call ADD CONSTRAINT ck_call_current_status_valid "
        f"CHECK (current_status IN ({_STATUSES_NEW}))"
    )
    # The one-live-call-per-form partial index bakes the terminal list into its
    # predicate: without 'canceled' there, a canceled call stays "live" and the
    # form can never be dialed again (unique violation on the retry dispatch).
    op.execute("DROP INDEX IF EXISTS uq_call_active_form")
    op.execute(
        "CREATE UNIQUE INDEX uq_call_active_form ON call (form_id) "
        f"WHERE current_status NOT IN ({_TERMINAL_NEW})"
    )


def downgrade() -> None:
    op.execute("UPDATE call SET current_status = 'failed' WHERE current_status = 'canceled'")
    op.execute("DROP INDEX IF EXISTS uq_call_active_form")
    op.execute(
        "CREATE UNIQUE INDEX uq_call_active_form ON call (form_id) "
        f"WHERE current_status NOT IN ({_TERMINAL_OLD})"
    )
    op.execute("ALTER TABLE call DROP CONSTRAINT IF EXISTS ck_call_current_status_valid")
    op.execute(
        "ALTER TABLE call ADD CONSTRAINT ck_call_current_status_valid "
        f"CHECK (current_status IN ({_STATUSES_OLD}))"
    )
    op.execute("ALTER TABLE call DROP CONSTRAINT IF EXISTS fk_call_end_requested_by_id_app_user")
    op.execute("ALTER TABLE call DROP COLUMN IF EXISTS end_requested_by_id")
```

- [ ] **Step 3: Verify on a fresh throwaway DB** — create `vera_mig_check` in the docker postgres, `VERA_DATABASE_URL=postgresql+asyncpg://vera:vera@localhost:5432/vera_mig_check uv run alembic upgrade head` → completes; then `... downgrade -1` and `... upgrade head` again → completes. Drop the DB.

- [ ] **Step 4: Migrate the branch dev DB** — `just migrate` → runs the new revision on `vera_voice_pipeline_integrate`.

- [ ] **Step 5: Commit** — `git add migrations && git commit -m "chore(db): canceled call status + end_requested_by_id (widen CHECK + active-form index)"`

---

### Task 3: `close_call` stamps end-intent (call_closeout.py)

**Files:**
- Modify: `apps/control_plane/src/control_plane/call_closeout.py` (`close_call`, line 31)

**Interfaces:**
- Produces: `close_call(sessionmaker, audit, room_name, status, *, trigger, actor_label="agent-worker", end_requested_by: UUID | None = None) -> RoomRef | None` — when `end_requested_by` is set, stamps `call.end_requested_by_id` inside the same locked transaction before applying the terminal status.

- [ ] **Step 1: Implement** (no separate unit file — Task 4's integration tests exercise it):

Signature gains the kwarg:
```python
async def close_call(
    sessionmaker: async_sessionmaker[AsyncSession],
    audit: AuditSink,
    room_name: str,
    status: CallStatus,
    *,
    trigger: str,
    actor_label: str = "agent-worker",
    end_requested_by: UUID | None = None,
) -> RoomRef | None:
```
(import `from uuid import UUID`), and right after the terminal-guard early return, before `apply_terminal_call_status`:
```python
        if end_requested_by is not None:
            call.end_requested_by_id = end_requested_by
```

- [ ] **Step 2: Gate** — `uv run pytest tests/unit/control_plane tests/integration/control_plane/test_calls.py -q` → PASS (no behavior change yet).

- [ ] **Step 3: Commit** — `git add apps && git commit -m "feat(calls): close_call can stamp the requesting user's end intent"`

---

### Task 4: end_call — pre-answer synchronous cancel, post-answer durable intent

**Files:**
- Modify: `apps/control_plane/src/control_plane/api/v1/calls.py` (`end_call`, ~line 389)
- Test: `tests/integration/control_plane/test_calls.py`

**Interfaces:**
- Consumes: `close_call(..., end_requested_by=...)` (Task 3), `CallStatus.CANCELED` (Task 1), `finalize_transcript(sessionmaker, call_stream, ref, room_name)`, `run_dispatch_pass(sessionmaker, tenant_id, livekit, kms, audit)`, deps `get_sessionmaker` / `get_call_stream_service` / `get_kms`.

- [ ] **Step 1: Write the failing tests** (append to `test_calls.py`; use the existing `seed_call` helper — it creates calls with `started_at` NULL and status `initiated`):

```python
@pytest.mark.asyncio
async def test_end_call_pre_answer_cancels_synchronously(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit: FakeLiveKit,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """End Call while dialing: no worker session exists, so no call.ended will
    ever arrive — the endpoint must close the call itself, as CANCELED."""
    call_id = await seed_call(
        admin_sessionmaker, rbac_world.tenant_id, seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
    )
    resp = await client.post(
        f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text
    row = (await admin_session.execute(select(Call).where(Call.id == call_id))).scalar_one()
    assert row.current_status == "canceled"
    assert row.end_requested_by_id == rbac_world.admin_id
    assert row.ended_at is not None
    assert room_name_for_call(rbac_world.tenant_id, call_id) in fake_livekit.deleted
    form = (
        await admin_session.execute(
            select(PatientForm).where(PatientForm.id == seeded_form_id)
        )
    ).scalar_one()
    assert form.status == "call_failed"  # parked for a human; NOT re-queued


@pytest.mark.asyncio
async def test_end_call_live_stamps_intent_and_defers_to_worker(
    client: httpx.AsyncClient,
    rbac_world: RBACWorld,
    seeded_form_id: UUID,
    fake_livekit: FakeLiveKit,
    admin_session: AsyncSession,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    call_id = await seed_call(
        admin_sessionmaker, rbac_world.tenant_id, seeded_form_id,
        initiated_by_id=rbac_world.admin_id,
    )
    async with tenant_session(admin_sessionmaker, rbac_world.tenant_id) as s:
        row = (await s.execute(select(Call).where(Call.id == call_id))).scalar_one()
        row.current_status = "active"
        row.started_at = func.now()
    resp = await client.post(
        f"/api/v1/calls/{call_id}/end", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code == 200, resp.text
    row = (await admin_session.execute(select(Call).where(Call.id == call_id))).scalar_one()
    assert row.current_status == "active"  # the worker's call.ended owns closeout
    assert row.end_requested_by_id == rbac_world.admin_id  # sweeper fallback: CANCELED
    assert room_name_for_call(rbac_world.tenant_id, call_id) in fake_livekit.deleted
```

(`func` import: `from sqlalchemy import func, select, ...` is already imported in the test file via `select`; add `func` if absent. `tenant_session` is already imported there.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/integration/control_plane/test_calls.py -k end_call -q` → the two new tests FAIL (status stays `initiated` / column missing).

- [ ] **Step 3: Rewrite `end_call`** (keep route decorator; new deps; docstring updated):

```python
async def end_call(
    call_id: UUID,
    request: Request,
    tenant_id: TenantId,
    session: TenantSession,
    livekit: LiveKit,
    audit: Audit,
    sessionmaker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    call_stream: Annotated[CallStreamService, Depends(get_call_stream_service)],
    kms: Annotated[KeyManagementService, Depends(get_kms)],
    caller: VerifiedIdentity = require("calls:read"),
) -> ResponseModel[None]:
    """End a call from Live Monitoring.

    LIVE call (answered — started_at set): stamp the caller's end intent on the
    row (durable: if the worker's call.ended never arrives, the sweeper closes
    the call as CANCELED instead of FAILED, so a user-ended call is never
    auto-redialed), then delete the room; the worker's shutdown emits call.ended
    and the consumer runs the one true closeout.

    PRE-ANSWER call (still dialing): no worker session exists, so no call.ended
    will ever come — close synchronously as CANCELED through the shared
    close_call path FIRST, then delete the room (order is load-bearing: room
    deletion makes the worker publish call.failed, which must find the row
    already terminal and no-op).

    Visibility matches join-token (`_call_hidden_from`): anyone who may watch
    the call may end it; a hidden call 404s so it is never revealed.
    """
    call = (
        await session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one_or_none()  # RLS already constrains to the caller's tenant
    if call is None or _call_hidden_from(call, caller.user_id):
        raise NotFoundError(message="call not found")
    if call.current_status in TERMINAL_VALUES:
        return ok(None, message="Call already ended.")
    pre_answer = call.started_at is None
    await audit.emit(
        AuditRecord(
            tenant_id=tenant_id,
            actor_type=ActorType.USER,
            actor_user_id=caller.user_id,
            actor_label=caller.email or caller.subject,
            event_type=AuditEvent.CALL_END.value,
            resource_type="call",
            resource_id=str(call.id),
            permission_key="calls:read",
            decision="allow",
            request_id=current_request_id(request),
            detail={
                "owner_id": str(call.initiated_by_id) if call.initiated_by_id else None,
                "phase": "pre_answer" if pre_answer else "live",
            },
        )
    )
    room_name = room_name_for_call(tenant_id, call.id)
    if pre_answer:
        ref = await close_call(
            sessionmaker,
            audit,
            room_name,
            CallStatus.CANCELED,
            trigger="user_end_call",
            actor_label=caller.email or caller.subject,
            end_requested_by=caller.user_id,
        )
        await livekit.delete_room(room_name)
        if ref is not None:  # freed a concurrency slot — let queued forms use it
            await finalize_transcript(sessionmaker, call_stream, ref, room_name)
            await run_dispatch_pass(sessionmaker, tenant_id, livekit, kms, audit)
        return ok(None, message="Call canceled.")
    async with tenant_session(sessionmaker, tenant_id) as stamp_session:
        locked = (
            await stamp_session.execute(
                select(Call).where(Call.id == call_id).with_for_update()
            )
        ).scalar_one_or_none()
        if locked is not None and locked.current_status not in TERMINAL_VALUES:
            locked.end_requested_by_id = caller.user_id
    await livekit.delete_room(room_name)
    return ok(None, message="Call is ending.")
```

Imports to add in `calls.py`: `from control_plane.call_closeout import TERMINAL_VALUES, close_call`, `from control_plane.deps import current_identity, get_call_stream_service, get_kms, get_sessionmaker`, `from control_plane.dispatch import run_dispatch_pass`, `from control_plane.transcript_finalizer import finalize_transcript`, `from vera_core.kms import KeyManagementService` (match the type `get_kms` returns — check its annotation and import from the same module it uses).

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/integration/control_plane/test_calls.py -q` → PASS, including the pre-existing end-call tests (they seed `initiated` calls, which now cancel synchronously — update any assertion that expected the status to remain `initiated` after end).

- [ ] **Step 5: Commit** — `git add -A apps tests && git commit -m "fix(calls): End Call cancels a dialing call synchronously; live calls carry durable end intent"`

---

### Task 5: Sweeper — CANCELED fallback + observer-only-room reaping

**Files:**
- Modify: `apps/control_plane/src/control_plane/livekit_gateway.py` (new method after `existing_rooms`)
- Modify: `apps/control_plane/src/control_plane/pipeline_sweeper.py` (`rooms_to_close`, `_sweep_tenant`)
- Modify: `tests/integration/control_plane/conftest.py` (FakeLiveKit)
- Test: `tests/unit/control_plane/test_pipeline_sweeper.py`

**Interfaces:**
- Produces: `LiveKitGateway.room_participant_identities(room_name: str) -> list[str] | None` (None = room gone). `rooms_to_close(rows: list[tuple[UUID, bool, bool]], live_rooms: set[str], observer_only_rooms: set[str], tenant_id: UUID) -> list[tuple[str, bool, CallStatus]]` where rows are `(call_id, past_cap, end_requested)` and results are `(room_name, delete_room_first, close_status)`.

- [ ] **Step 1: Write the failing tests** (extend the existing pure-function tests in `test_pipeline_sweeper.py`; keep their fixture style):

```python
def test_end_requested_closes_as_canceled() -> None:
    cid = uuid4()
    out = rooms_to_close([(cid, False, True)], set(), set(), TENANT)
    assert out == [(room_name_for_call(TENANT, cid), False, CallStatus.CANCELED)]


def test_worker_death_closes_as_failed() -> None:
    cid = uuid4()
    out = rooms_to_close([(cid, False, False)], set(), set(), TENANT)
    assert out == [(room_name_for_call(TENANT, cid), False, CallStatus.FAILED)]


def test_observer_only_live_room_is_reaped_with_delete() -> None:
    cid = uuid4()
    room = room_name_for_call(TENANT, cid)
    out = rooms_to_close([(cid, False, False)], {room}, {room}, TENANT)
    assert out == [(room, True, CallStatus.FAILED)]


def test_live_room_with_speaker_within_cap_left_alone() -> None:
    cid = uuid4()
    room = room_name_for_call(TENANT, cid)
    assert rooms_to_close([(cid, False, False)], {room}, set(), TENANT) == []
```

(Adjust existing `rooms_to_close` tests in that file to the new 3-tuple rows / 3-tuple results.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/control_plane/test_pipeline_sweeper.py -q` → FAIL (signature mismatch).

- [ ] **Step 3: Implement**

`livekit_gateway.py` (after `existing_rooms`):
```python
    async def room_participant_identities(self, room_name: str) -> list[str] | None:
        """Identities currently in the room, or None when the room doesn't exist.
        The sweeper uses this to spot dead-but-open rooms: a room holding only
        browser observers (supervisor-*/monitor-*) has no agent and no SIP callee,
        so the call can never progress — but the observers keep the room's
        departure timeout from ever firing."""
        async with self._client() as lk:
            try:
                resp = await lk.room.list_participants(
                    api.ListParticipantsRequest(room=room_name)
                )
            except TwirpError as exc:
                if exc.code == "not_found":
                    return None
                raise
        return [p.identity for p in resp.participants]
```

`conftest.py` FakeLiveKit — add to `__init__`: `self.participants: dict[str, list[str]] = {}`, and:
```python
    async def room_participant_identities(self, room_name: str) -> list[str] | None:
        return self.participants.get(room_name)
```

`pipeline_sweeper.py`:
```python
from vera_core.observability.correlation import is_observer_identity, room_name_for_call


def rooms_to_close(
    rows: list[tuple[UUID, bool, bool]],
    live_rooms: set[str],
    observer_only_rooms: set[str],
    tenant_id: UUID,
) -> list[tuple[str, bool, CallStatus]]:
    """Which stuck-call candidates to close: `(room_name, delete_room_first, status)`.

    rows: (call_id, past_cap, end_requested) for non-terminal calls past the grace
    window. Room gone → close. Room live but past the hard cap, or held open only
    by browser observers (no agent, no SIP callee — the call can never progress) →
    delete the room first, then close. Otherwise a long call still in progress —
    leave it alone. A call whose end was user-requested closes as CANCELED (never
    auto-redialed); everything else is FAILED.
    """
    result: list[tuple[str, bool, CallStatus]] = []
    for call_id, past_cap, end_requested in rows:
        room_name = room_name_for_call(tenant_id, call_id)
        status = CallStatus.CANCELED if end_requested else CallStatus.FAILED
        if room_name not in live_rooms:
            result.append((room_name, False, status))
        elif past_cap or room_name in observer_only_rooms:
            result.append((room_name, True, status))
    return result
```

`_sweep_tenant` phase 1 select gains the flag:
```python
            stuck_candidates = await session.execute(
                select(
                    Call.id,
                    (Call.created_at < func.now() - cap).label("past_cap"),
                    Call.end_requested_by_id.is_not(None).label("end_requested"),
                ).where(
                    Call.tenant_id == tenant_id,
                    Call.current_status.not_in(list(TERMINAL_VALUES)),
                    Call.created_at < func.now() - grace,
                )
            )
            rows = [(row.id, row.past_cap, row.end_requested) for row in stuck_candidates.all()]
```

phase 2 becomes:
```python
        closed = 0
        if rows:
            candidate_rooms = [room_name_for_call(tenant_id, cid) for cid, _, _ in rows]
            live_rooms = await self._livekit.existing_rooms(candidate_rooms)
            observer_only: set[str] = set()
            for room_name in sorted(live_rooms):
                identities = await self._livekit.room_participant_identities(room_name)
                if identities is None:
                    live_rooms.discard(room_name)  # vanished between the two probes
                elif all(is_observer_identity(i) for i in identities):
                    # empty, or only supervisors/monitors — nothing can progress
                    observer_only.add(room_name)
            for room_name, delete_first, status in rooms_to_close(
                rows, live_rooms, observer_only, tenant_id
            ):
                if delete_first:
                    logger.warning(
                        "sweeper: room %s is dead (past cap or observer-only); deleting",
                        room_name,
                    )
                    await self._livekit.delete_room(room_name)
                ref = await close_call(
                    self._sessionmaker,
                    self._audit,
                    room_name,
                    status,
                    trigger="sweeper_reconcile",
                    actor_label="pipeline-sweeper",
                )
                if ref is not None:
                    await finalize_transcript(self._sessionmaker, self._call_stream, ref, room_name)
                    closed += 1
                    logger.info("sweeper: reconciled stuck call room %s as %s", room_name, status.value)
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/unit/control_plane/test_pipeline_sweeper.py tests/integration/control_plane -q` → PASS.

- [ ] **Step 5: Commit** — `git add -A apps tests && git commit -m "feat(sweeper): close user-ended calls as canceled; reap live rooms held open only by observers"`

---

### Task 6: Worker — wait_for_speaker resolves on room disconnect

**Files:**
- Modify: `apps/agent_worker/src/agent_worker/main.py` (`wait_for_speaker`, lines 111-179)
- Test: `tests/unit/worker/test_wait_for_speaker.py`

**Interfaces:**
- Consumes: existing `CallFailed(CallFailureReason.FAILED)` outcome; the entrypoint already publishes `call.failed` for any `CallFailed` outcome — the consumer's terminal-guard + not_found-tolerant room ops make that publish a safe no-op when the control plane already closed the call.

- [ ] **Step 1: Write the failing test** (follow the fake-room pattern already in `test_wait_for_speaker.py`):

```python
async def test_room_disconnect_resolves_failed() -> None:
    """Room deleted mid-dial (user cancel / sweeper): the wait must resolve
    immediately so the entrypoint exits cleanly and publishes an outcome,
    instead of hanging until the framework force-cancels it."""
    ctx = _ctx_with_participants({})  # use the file's existing fake JobContext builder
    task = asyncio.create_task(wait_for_speaker(ctx, timeout_s=30))
    await asyncio.sleep(0)
    ctx.room.emit("disconnected", None)
    outcome = await asyncio.wait_for(task, timeout=1)
    assert isinstance(outcome, CallFailed)
    assert outcome.reason is CallFailureReason.FAILED
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/unit/worker/test_wait_for_speaker.py -q` → the new test times out / FAILS.

- [ ] **Step 3: Implement** — in `wait_for_speaker`, alongside the existing handlers:

```python
    def _on_room_disconnected(reason: object = None) -> None:
        logger.info(
            "wait_for_speaker[%s]: room disconnected (%s) — resolving as failed",
            ctx.room.name,
            reason,
        )
        if not result.done():
            result.set_result(CallFailed(CallFailureReason.FAILED))

    ctx.room.on("disconnected", _on_room_disconnected)
```
and in the `finally` block:
```python
        ctx.room.off("disconnected", _on_room_disconnected)
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/unit/worker tests/unit/agent_worker -q` → PASS.

- [ ] **Step 5: Commit** — `git add -A apps tests && git commit -m "fix(worker): wait_for_speaker resolves on room disconnect instead of hanging into force-cancel"`

---

### Task 7: Explicit room lifetimes on create_call_room

**Files:**
- Modify: `apps/control_plane/src/control_plane/livekit_gateway.py` (`create_call_room`, line 57)
- Test: `tests/integration/control_plane/test_livekit_gateway.py`

**Interfaces:**
- Produces: rooms created with `empty_timeout=300`, `departure_timeout=120` (module constants `_ROOM_EMPTY_TIMEOUT_S`, `_ROOM_DEPARTURE_TIMEOUT_S`).

- [ ] **Step 1: Write the failing test** (follow `test_livekit_gateway.py`'s existing pattern for asserting outgoing requests — it stubs `api.LiveKitAPI`; if it only tests token minting, add a stub that captures `create_room`'s request):

```python
async def test_create_call_room_sets_room_lifetimes(monkeypatch) -> None:
    captured: list[api.CreateRoomRequest] = []
    # reuse/extend the file's fake LiveKitAPI double to append to `captured`
    gw = LiveKitGateway(url="ws://x", api_key="k", api_secret="s")
    # ... patch gw._client to yield the double ...
    await gw.create_call_room("call--t--c")
    req = captured[0]
    assert req.empty_timeout == 300
    assert req.departure_timeout == 120
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement** — module constants + request:

```python
# Belt-and-suspenders room lifetimes (the sweeper is the primary net):
# empty_timeout: a room nobody ever joined (dispatch crashed before the dial)
# self-deletes; departure_timeout: a room lingers only briefly once the last
# participant leaves. NOTE: a watching supervisor counts as a participant, so
# neither fires for observer-held rooms — the sweeper's observer-only probe
# handles those.
_ROOM_EMPTY_TIMEOUT_S = 300
_ROOM_DEPARTURE_TIMEOUT_S = 120
```
```python
            await lk.room.create_room(
                api.CreateRoomRequest(
                    name=room_name,
                    empty_timeout=_ROOM_EMPTY_TIMEOUT_S,
                    departure_timeout=_ROOM_DEPARTURE_TIMEOUT_S,
                )
            )
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/integration/control_plane/test_livekit_gateway.py -q` → PASS.

- [ ] **Step 5: Commit** — `git add -A apps tests && git commit -m "feat(livekit): explicit empty/departure timeouts on call rooms"`

---

### Task 8: Frontend — canceled is a terminal status

**Files:**
- Modify: `vera-frontend/src/lib/api/callEvents.ts` (TERMINAL_CALL_STATUSES set)
- Test: `vera-frontend/src/lib/api/callEvents.test.ts`

- [ ] **Step 1: Failing test** — in the `isTerminalCallStatus` describe block, add `"canceled"` to the terminal `it.each` list. Run `npx vitest run src/lib/api/callEvents.test.ts` → FAIL.

- [ ] **Step 2: Implement:**
```typescript
const TERMINAL_CALL_STATUSES = new Set([
  "ended",
  "completed",
  "failed",
  "no_answer",
  "busy",
  "canceled",
])
```

- [ ] **Step 3: Verify** — `npx vitest run src/lib/api/callEvents.test.ts` → PASS.

- [ ] **Step 4: Commit** — `git add vera-frontend/src/lib/api && git commit -m "fix(frontend): canceled counts as a terminal call status (Call-ended banner)"` (run from repo root).

---

### Task 9: Full gates, simplifier, verification

- [ ] **Step 1:** Backend gate — `just check` from `vera-backend/` → green.
- [ ] **Step 2:** Frontend gate — `npm run lint && npm test && npm run build` from `vera-frontend/` → green.
- [ ] **Step 3:** Run the code-simplifier agent on the whole change (repo rule); re-run both gates if it edits anything.
- [ ] **Step 4:** End-to-end check of the original bug, DB-level: with `just api` running, seed a call via the dispatcher (or `seed_call`-style insert), hit `POST /calls/{id}/end` pre-answer, and confirm in psql: `current_status='canceled'`, `end_requested_by_id` set, form `call_failed`, no new call row appears afterward (no redial).
- [ ] **Step 5:** Commit any residue and push the branch (PR #80).
