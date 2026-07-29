# Per-VA concurrent-agent limits — design

**Date:** 2026-07-28
**Status:** approved
**Story:** As a tenant admin, I want to set how many agents each VA can run
concurrently, so that I can tune capacity and tone for my team.

## Acceptance criteria

1. Admin can set a max-concurrent-agents value per VA; the limit is enforced at
   call initiation.
2. Exceeding the limit returns a clear error rather than failing silently.
3. Config changes are scoped to the tenant and audited.

## Decisions made during brainstorming

- **One tenant-level setting applied per VA** (not per-VA individual overrides):
  the admin sets a single number; every VA is independently capped at it.
- **In-flight = queued + active.** A VA's slot count is their forms in
  `in_queue` + `in_call` + `ai_processing` (`enqueued_by_id = VA`). The limit
  is enforced synchronously at the enqueue endpoint — the VA's "start a call"
  action — with a clear 409 error. The dispatcher needs no per-VA logic.
- **Separate tenant-wide ceiling.** The dispatcher's existing tenant-wide slot
  math moves to a new `max_concurrent_calls` column so per-VA limits can't
  multiply total tenant capacity unbounded. Backfilled from the current
  `max_agents_per_va` value so live tenants keep exactly today's capacity.
- **Full stack**: backend API + frontend admin settings UI.

## Current state (what exists today)

- `Tenant.max_agents_per_va` (Integer, NOT NULL, default 3) exists but is
  misleadingly enforced **tenant-wide**: `queue_dispatcher.py` computes
  `slots = tenant.max_agents_per_va - active_count` where `active_count` counts
  ALL the tenant's forms in `_ACTIVE_FORM_STATUSES` (`in_call`,
  `ai_processing`).
- Call initiation is asynchronous: a VA enqueues a form
  (`PUT /patient-forms/{form_id}/status` → `in_queue`, stamping
  `enqueued_by_id`); the background dispatcher dials FIFO and stamps
  `initiated_by_id = form.enqueued_by_id`.
- Admin tenant-config pattern exists in `api/v1/tenant_config.py`
  (persona/retention): `require("tenant:config:manage")` or similar, RLS-scoped
  `TenantSession`, `emit_auth_event` auth-audit with non-PHI meta.
- No admin endpoint or frontend UI exposes either concurrency knob.

## Design

### 1. Data model & migration

`packages/vera_core/src/vera_core/models/tenant.py`:

- `max_agents_per_va` — **unchanged column, corrected meaning**: max in-flight
  forms per VA. Only enforcement changes.
- `max_concurrent_calls` — new `Mapped[int]`, `Integer, nullable=False,
  default=25`: tenant-wide dial ceiling used by the dispatcher.

New Alembic migration (ID via `just makemigration`; never hand-numbered),
idempotent against both DB shapes (fresh-CI DB where `0001`'s `create_all`
already created the column vs. provisioned dev DB):

1. `ADD COLUMN IF NOT EXISTS max_concurrent_calls integer` (nullable, no
   default).
2. Backfill: `UPDATE tenant SET max_concurrent_calls = max_agents_per_va WHERE
   max_concurrent_calls IS NULL` — preserves each live tenant's current dial
   capacity.
3. `ALTER COLUMN max_concurrent_calls SET NOT NULL`, `SET DEFAULT 25`.

New partial index on `patient_form`:

```sql
CREATE INDEX IF NOT EXISTS ix_patient_form_in_flight
ON patient_form (tenant_id, enqueued_by_id)
WHERE status IN ('in_queue', 'in_call', 'ai_processing');
```

Serves the per-VA in-flight count and the dispatcher's tenant-wide active
count; stays small because terminal forms fall out of the predicate.

### 2. Enqueue gate — the user-facing enforcement (AC1 + AC2)

New helper `ensure_va_capacity(session, tenant, caller_user_id)` in
`apps/control_plane/src/control_plane/queueability.py`, called from
`api/v1/patient_forms.py::update_patient_form_status` when
`target == FormStatus.IN_QUEUE`, immediately after `ensure_queueable`:

1. `SELECT pg_advisory_xact_lock(<ENQUEUE_LOCK_CLASS>,
   hashtext('<tenant_id>:<user_id>'))` — transaction-scoped, serializes
   concurrent enqueues by the same VA only (double-click race); a distinct lock
   class constant from the dispatcher's `_DISPATCH_LOCK_CLASS`.
2. Count the caller's in-flight forms: `PatientForm.tenant_id == tenant.id AND
   enqueued_by_id == caller_user_id AND status IN ('in_queue', 'in_call',
   'ai_processing')`. Note this set is `_ACTIVE_FORM_STATUSES` + `in_queue` —
   define it once next to the helper.
3. If `count >= tenant.max_agents_per_va`, raise
   `CustomAPIException(DefaultExceptionCode.CONFLICT)` with message
   `"You are at your concurrent-agent limit ({limit}). Wait for a call to
   finish or ask your admin to raise the limit."` and
   `data={"limit": limit, "in_flight": count}`. Non-PHI.

System re-enqueues are **not** gated: the auto-retry transition
(`ai_processing → in_queue` in `post_call.py` / post-call eval) keeps
`enqueued_by_id` and does not grow the VA's in-flight count, and a background
consumer has no user to error to. No change there.

### 3. Dispatcher ceiling

`packages/vera_core/src/vera_core/services/queue_dispatcher.py`: the slot
computation becomes `slots = tenant.max_concurrent_calls - active_count`.
Nothing else changes (per-tenant advisory lock, FIFO `SKIP LOCKED` fetch,
dial pacing, expiry handling all stay as-is). The dispatcher stays per-VA
oblivious — the enqueue gate already caps each VA's contribution to the queue.

### 4. Admin config API (AC3)

Two endpoints in `api/v1/tenant_config.py`, mirroring the retention pattern:

- `GET /tenant/config/concurrency` → `ok(ConcurrencyConfig)`,
  `require("tenant:config:manage")`.
- `PATCH /tenant/config/concurrency`, same gate, body
  `ConcurrencyConfigUpdate` (both fields optional so each knob changes
  independently); returns the updated `ConcurrencyConfig`.

Schemas in `packages/vera_core/src/vera_core/schemas/dto.py` (exported via
`schemas/__init__.py`):

```python
class ConcurrencyConfig(BaseModel):
    max_agents_per_va: int       # ge=1, le=20
    max_concurrent_calls: int    # ge=1, le=100

class ConcurrencyConfigUpdate(BaseModel):
    max_agents_per_va: int | None = None       # ge=1, le=20
    max_concurrent_calls: int | None = None    # ge=1, le=100
```

No cross-field constraint: a ceiling below the per-VA limit is valid (the
ceiling binds first); the UI hints, the API does not block.

Tenant scoping is inherited: `TenantSession` + RLS restricts the handler to the
caller's own tenant row — identical to persona/retention.

Audit: new `AuthEvent.CONCURRENCY_CONFIG_UPDATED` member
(`vera_core/models/enums.py`), emitted via `emit_auth_event` with
`meta={"old": {...}, "new": {...}}` carrying before/after values (non-PHI
config numbers — same precedent as `RETENTION_POLICY_UPDATED`'s
`old_days`/`new_days`). Never a hand-rolled `AuthAuditRecord`.

### 5. Frontend

- `src/lib/api/tenantConfig.ts` — `getConcurrencyConfig()` /
  `patchConcurrencyConfig()` following the existing per-domain API module +
  test pattern (`client.ts` fetch, envelope unwrap, error mapping).
- `src/components/settings/ConcurrencySection.tsx` — rendered from
  `pages/Settings.tsx` in a `SettingsCard` titled "Agent capacity". Two number
  inputs (bounds mirroring the API), save button, saving/saved state, and any
  API error envelope `message` shown inline. A hint (not a blocker) when
  `max_concurrent_calls < max_agents_per_va`.
- Section visibility gates on the `tenant:config:manage` permission the same
  way existing admin-only sections do.
- The forms-queueing UI surfaces the new 409's `message` as the error toast —
  the VA-facing half of AC2.

### 6. Testing

Backend unit (`tests/unit/`):
- `ensure_va_capacity`: below limit passes; at limit raises CONFLICT with
  `limit`/`in_flight` data; counts only the caller's forms; counts only
  in-flight statuses (terminal/review statuses ignored).
- Dispatcher: slot math reads `max_concurrent_calls`; per-VA column no longer
  consulted there.
- Config endpoints: RBAC deny without `tenant:config:manage`; bounds
  validation (0, 21, 101 rejected); partial PATCH updates one knob; audit
  event emitted with old/new meta.

Backend integration (`tests/integration/`):
- VA at limit: enqueue N forms → N+1th returns 409 with structured data;
  a second VA can still enqueue.
- Slot frees: end a call (or transition a form out of in-flight) → enqueue
  succeeds.
- Race: two concurrent enqueues at limit-1 → exactly one succeeds (advisory
  lock).
- Migration backfill: tenant with `max_agents_per_va=5` gets
  `max_concurrent_calls=5`.

Frontend: API module tests (get/patch, error mapping); section renders values,
save issues PATCH, API error message rendered.

Gates: backend `just check`; frontend `tsc` + `eslint` + tests + build;
`/simplify` pass before commit (repo rule).

## Risks & trade-offs

- A VA at their limit cannot queue anything until a slot frees — intentional
  per the in-flight decision, but a behavior change from today's unlimited
  queueing; release notes should tell admins.
- `max_agents_per_va` keeps its name and finally means what it says; the
  migration backfill is what protects existing tenants from a capacity jump
  when the dispatcher switches to `max_concurrent_calls`.
- `default=25` applies only to tenants created after the migration; existing
  tenants get their backfilled (current) value.
- Forms enqueued before this ships with `enqueued_by_id IS NULL` (if any
  exist) never count toward any VA's limit — acceptable; they drain normally.

## Out of scope

- Per-VA individual override values (tenant default + override was considered
  and rejected for now).
- Surfacing "dispatcher skipped you due to tenant ceiling" to VAs — the queue
  position/status UI is unchanged.
- Platform-level (cross-tenant) capacity caps.
