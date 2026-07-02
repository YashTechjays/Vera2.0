# Publish a call to other VAs — private-by-default calls, tenant-wide visibility, intervene

**Date:** 2026-07-02
**Status:** Approved design, ready for implementation planning
**Implement from:** `main` (feature built on the persistent `/calls` path).

## 1. Purpose

Today a verification call initiated by a VA is not scoped to that VA — the active-call
list (`GET /calls`) returns **every** active call in the tenant, and any holder of
`calls:read` can mint a join token for any of them. There is no notion of *ownership*
or *private vs. shared*.

This feature introduces that axis. A concurrent call is **private to the initiating VA**
by default. The owner can **publish** it, which makes it visible **tenant-wide** so any
VA in the tenant can view it and **intervene** — join the live LiveKit room to **listen
and talk**. Publishing is **one-way** (a published call never returns to private) and is
treated as a **PHI-disclosure decision**: it widens who can hear the live transcript, so
both the publish and each non-owner join are recorded in the compliance audit trail.

The feature is built on the persistent `/calls` path **only**. Voice Lab
(`/voice-lab/sessions`) is out of scope: it deliberately writes no `Call` row and has no
owner, so there is nothing to publish (see `2026-06-23-voice-lab-session-design.md` §2).

### Settled decisions (tech lead)

1. **Substrate** — build on the persistent `/calls` path only; Voice Lab excluded.
2. **Intervention capability** — **listen + talk only** for v1. Richer typed actions
   (flag / coach / whisper / takeover, DTMF, form takeover) are deferred.
3. **Visibility scope** — **tenant-wide**. A published call is visible to all VAs in the
   owner's tenant. Publishing never crosses a tenant boundary (RLS still isolates tenants).
4. **One-way + owner master access** — once published, a call **cannot** be turned back to
   private (un-publishing mid-intervention would leave a bad state). The owner instead keeps
   master access to **revoke a specific non-owner's access** (eject the intervener) without
   changing the published state. `initiated_by_id` never changes (no takeover in v1).
5. **Authz + audit** — a dedicated `calls:publish` permission (Supervisor / Admin) gates the
   publish endpoint; publish and each non-owner join are written to the compliance audit log.

### Open item for the tech lead

- **`calls:publish` vs. reusing `calls:write`.** `calls:write` already exists
  (`rbac_defaults.py`, "Create and manage verification calls") and is already held by exactly
  `TENANT_ADMIN` + `SUPERVISOR` — the same audience. This spec adds a **dedicated**
  `calls:publish` because publishing widens PHI disclosure and the backend guardrails want a
  disclosure-widening action behind *its own* permission, not folded into a broad write
  permission. Collapsing onto `calls:write` is a valid alternative if the tech lead prefers
  fewer permissions. Decision affects §4.2 only.
- **Propagation = polling for v1** (chosen as the default while awaiting confirmation). Other
  VAs learn a call was published by the monitoring page **polling `GET /calls`** (~5–10s). An
  SSE push (reusing the transcript SSE pattern) is a documented follow-up (§8). Confirm polling
  is acceptable for v1.

## 2. Scope

### In scope

- **Backend**
  - Set `Call.initiated_by_id = caller.user_id` on create (currently left unset).
  - Add a one-way visibility axis to `Call`: `published` (bool) + `published_at` (timestamptz)
    columns + a `(tenant_id, published)` index (new Alembic migration).
  - `POST /calls/{call_id}/publish` — owner-only, idempotent, one-way; gated by `calls:publish`.
  - Scope `GET /calls`: `initiated_by_id == caller OR published` (instead of all active calls).
  - Gate `GET /calls/{call_id}/join-token`: a non-owner may mint a token **only** for a
    published call; audit the non-owner join as a PHI disclosure.
  - `POST /calls/{call_id}/revoke-access` — owner-only; ejects a named participant from the
    room (new `LiveKitGateway.remove_participant`) and audits it.
  - Add `calls:publish` to the permission catalog and grant it to `TENANT_ADMIN` + `SUPERVISOR`.
  - Wire the PHI `AuditSink` into the calls router (not currently wired there).
- **Frontend**
  - A `callsSlice` in Redux for call state (today call state is local component state; only
    `authSlice` exists).
  - A one-way **Publish** button in the owner's active-session UI (`SessionPanel`).
  - Point Live Monitoring at the real `GET /calls` (replace `mock-data.ts`); the
    "Visible To All" column becomes read-only published state; poll for propagation.
  - Wire the **View Live / Intervene** modals to `join-token` so a second VA joins the room;
    support viewing another VA's published room (today the browser hosts a single `<LiveKitRoom>`).

### Out of scope / non-goals

- **Voice Lab publishing** — no `Call` row, no owner; excluded.
- **Un-publish** — publishing is permanent by decision (4).
- **Typed intervention actions** — flag / coach / whisper / takeover, form takeover, DTMF, and
  the `POST /calls/{id}/interventions` endpoint that records `InterventionEvent` rows. v1
  intervention is exactly "join the room and listen/talk," which the gated `join-token` already
  provides. (The `intervention_event` table and `InterventionType` enum already exist, unused.)
- **Cross-tenant visibility** — forbidden; RLS boundary is unchanged.
- **SSE / websocket push** for publish propagation — polling for v1 (§8 follow-up).
- **Ownership handoff / takeover semantics** — `initiated_by_id` is immutable in v1.

## 3. Starting point — what `main` already provides

Confirmed present; reuse directly:

- **`Call.initiated_by_id`** already exists as a nullable FK to `app_user` with index
  `ix_call_initiated_by` on `(initiated_by_id, created_at)` (`models/call.py:52`). Only the
  *write* is missing — `start_call` never sets it. **No migration needed for the owner column.**
- **`api/v1/calls.py`** — `start_call`, `join_token`, `list_calls`, and the `_summary` helper,
  plus the `LiveKit` / `TenantId` / `TenantSession` DI aliases in `api/v1/common.py`.
- **`LiveKitGateway`** — `create_call_room`, `mint_join_token(room_name, identity)`,
  `create_sip_participant`. Room name via `room_name_for_call(tenant_id, call_id)`.
- **Compliance audit** — `vera_core.audit.AuditRecord` + `AuditSink.emit` (`audit/writer.py`),
  the append-only `audit_log` HIPAA trail (records field **names**/ids, never values;
  timestamps from the DB clock).
- **RBAC catalog** — `vera_core/models/rbac_defaults.py` (`DEFAULT_PERMISSIONS`, `SYSTEM_ROLES`)
  and `control_plane/auth/rbac.py`'s `require(...)`.
- **`intervention_event`** table + `InterventionType` enum (`models/oversight.py`,
  `models/enums.py`) — already modeled, currently unused; reserved for the deferred typed actions.

### Current stopgap this feature partially closes

`calls.py` guards all three endpoints with `require("calls:read")` (acknowledged in its
docstring) and does no PHI-access audit. This spec tightens **publish** and **revoke** to
their own permissions and adds the disclosure audit for publish + non-owner join. The broader
"every calls endpoint should audit PHI and use a write/manage permission" cleanup remains a
separate task; this spec only does what the feature needs.

## 4. Architecture

Five backend units + the frontend, each independently testable.

### 4.1 Data model — `models/call.py` + migration

Add to `Call` (visibility is orthogonal to `current_status`):

```python
# Visibility axis — orthogonal to current_status. One-way: once True it never
# returns to False (see spec §1 decision 4). Default False = private to the owner.
published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Add to `__table_args__` the scan index for "show me published calls":

```python
Index("ix_call_tenant_published", "tenant_id", "published")
```

New Alembic migration (auto-generated id, date-prefixed — never hand-numbered; see
`vera-backend/CLAUDE.md`): add the two columns + the index. `published` defaults `False`, so
existing rows migrate cleanly as private. The one-way invariant is enforced in the endpoint
(§4.3), not by a DB constraint.

### 4.2 Authz — `models/rbac_defaults.py`

Add to `DEFAULT_PERMISSIONS`:

```python
"calls:publish": "Publish a call so other VAs in the tenant can view and intervene",
```

Grant it by adding `"calls:publish"` to the `SUPERVISOR` frozenset. `TENANT_ADMIN` holds all
of `DEFAULT_PERMISSIONS` and `SUPER_ADMIN` holds `ALL_PERMISSIONS`, so both pick it up
automatically. Re-seeding the catalog (`scripts/seed.py`) grants it to the seeded roles.

*(If the tech lead chooses to reuse `calls:write` instead — see §1 open item — skip this
section and gate the publish endpoint with `require("calls:write")`.)*

### 4.3 Endpoints — `api/v1/calls.py`

**(1) `start_call` — set the owner.** Add `initiated_by_id=caller.user_id` to the `Call(...)`
construction (the `_caller` dep is already present; rename to `caller` to use its id).

**(2) `POST /calls/{call_id}/publish` — one-way publish (owner-only).**

```python
@router.post("/calls/{call_id}/publish", response_model=ResponseModel[CallSummary], ...)
async def publish_call(
    call_id: UUID,
    tenant_id: TenantId,
    session: TenantSession,
    audit: Audit,                                 # new DI alias, §4.4
    caller: VerifiedIdentity = require("calls:publish"),
) -> ResponseModel[CallSummary]:
    call = (await session.execute(select(Call).where(Call.id == call_id))).scalar_one_or_none()
    if call is None:
        raise NotFoundError(message="call not found")          # RLS already scopes to tenant
    if call.initiated_by_id != caller.user_id:
        raise CustomAPIException(DefaultExceptionCode.FORBIDDEN, message="only the owner can publish")
    if not call.published:                                     # idempotent, one-way
        call.published = True
        call.published_at = func.now()                         # DB clock
        await audit.emit(AuditRecord(... event_type="call.publish", resource_id=str(call.id) ...))
    return ok(_summary(call, None))
```

No request body, no un-publish branch. A second publish is a no-op that emits no extra audit.

**(3) `GET /calls` — scope the list.** Add an ownership/visibility filter to the existing query:

```python
.where(or_(Call.initiated_by_id == caller.user_id, Call.published.is_(True)))
```

(Requires reading the caller id — swap `_caller` for `caller`.) Tenant-wide is automatic:
RLS already constrains rows to the caller's tenant, so "published" means "published within my
tenant." `CallSummary` gains `published` (and `is_owner`) fields so the UI can render state.

**(4) `GET /calls/{call_id}/join-token` — gate + audit the non-owner join.**

```python
is_owner = call.initiated_by_id == caller.user_id
if not is_owner and not call.published:
    raise NotFoundError(message="call not found")   # don't reveal a private call's existence
if not is_owner:
    await audit.emit(AuditRecord(... event_type="call.intervene.join",
                                 resource_id=str(call.id), ...))   # PHI disclosure
identity = f"supervisor-{caller.user_id}"            # listen + talk grants
token = livekit.mint_join_token(room_name=room_name, identity=identity)
```

The `supervisor-{user_id}` identity already carries publish+subscribe audio grants, so "listen
+ talk" needs no additional endpoint — joining the room *is* the v1 intervention.

**(5) `POST /calls/{call_id}/revoke-access` — owner ejects an intervener.**

```python
@router.post("/calls/{call_id}/revoke-access", ...)
async def revoke_access(call_id, body: RevokeAccessRequest,       # { target_user_id: UUID }
                        tenant_id, session, livekit, audit,
                        caller = require("calls:publish")):
    # owner-only; call must exist + be published
    await livekit.remove_participant(room_name, f"supervisor-{body.target_user_id}")
    await audit.emit(AuditRecord(... event_type="call.intervene.revoke", ...))
```

Requires a new `LiveKitGateway.remove_participant(room_name, identity)` wrapping the LiveKit
`RoomService.remove_participant` API (exact request shape verified against the installed
`livekit-api` version at implementation). The call **stays published**.

### 4.4 Audit — wire the PHI `AuditSink` into the calls router

The calls router has no PHI-audit dependency today. Add an `Audit` DI alias in
`api/v1/common.py` mirroring the existing `AuthAudit` alias:

```python
Audit = Annotated[AuditSink, Depends(get_audit)]   # get_audit from control_plane.deps
```

Three audit events, all via `AuditRecord` → `audit_log` (field names/ids only, never transcript
values; DB-clock timestamp; append-only):

| Event | `event_type` | When |
|---|---|---|
| Publish | `call.publish` | owner publishes a private call (first time only) |
| Non-owner join | `call.intervene.join` | a non-owner mints a join token for a published call |
| Revoke | `call.intervene.revoke` | owner ejects an intervener |

`detail` carries the call id, owner id, and (for join/revoke) the acting/target user id — all
non-PHI identifiers. The publish/join **operational** markers on the call's own timeline
(`CallEvent`) are optional and not required by this feature; the compliance record is the
`audit_log` entry.

### 4.5 Frontend — `vera-frontend`

- **API client** (`src/lib/api/calls.ts`, mirroring `voiceLab.ts`): `listCalls()`,
  `publishCall(callId)`, `getJoinToken(callId)`, `revokeAccess(callId, targetUserId)`.
- **State** — a `callsSlice` in Redux holding the active list + per-call `published` / `is_owner`.
- **Publish control** — a one-way **Publish** button in the owner's `SessionPanel` actions
  (`VoiceLab.tsx`); calls `publishCall`, then disables and shows the published state (no un-publish).
- **Live Monitoring** — replace `mock-data.ts` with real `listCalls()`; the "Visible To All"
  column becomes read-only published state. **Poll** `listCalls()` (~5–10s) so other VAs learn
  about newly published calls (propagation, §8 for the push follow-up).
- **View Live / Intervene** — wire the modals to `getJoinToken` → join via `<LiveKitRoom>` with
  mic enabled (listen + talk). Support viewing another VA's published room; the browser currently
  hosts a single `<LiveKitRoom>`, so this needs to swap/host the joined room.

## 5. Data flow

**Initiate → publish → intervene → revoke:**

```
VA-1: POST /calls {form_id}
  → Call(initiated_by_id=VA1, published=false); room created; agent dispatched
  → GET /calls for VA-2 does NOT include this call (private)

VA-1: POST /calls/:id/publish            (calls:publish, owner-only, one-way)
  → published=true, published_at=now(); audit call.publish

VA-2: GET /calls  (polled)               → list now includes VA-1's call (published, tenant-wide)
VA-2: GET /calls/:id/join-token          → owner-or-published check passes; audit call.intervene.join
  → supervisor-VA2 token (listen + talk) → VA-2 joins the LiveKit room

VA-1 (optional): POST /calls/:id/revoke-access {target: VA2}
  → LiveKit remove_participant(supervisor-VA2); audit call.intervene.revoke
  → call stays published
```

## 6. Error handling

| Condition | Result |
|---|---|
| Publish by a non-owner | `403` ("only the owner can publish") |
| Publish without `calls:publish` | `403` from `require(...)` |
| Publish an already-published call | `200`, idempotent no-op, no extra audit |
| Publish / join / revoke on a missing call | `404` ("call not found") |
| Non-owner join-token on a **private** call | `404` (indistinguishable from missing — no enumeration) |
| Revoke by a non-owner | `403` |
| Revoke a participant not in the room | LiveKit no-op / surfaced as the gateway result; audit still records the attempt |
| Cross-tenant access | Impossible — RLS returns zero rows → `404` |

## 7. Testing

Mirror `tests/integration/control_plane/test_calls.py`.

- **List scoping** — owner sees their own private call; a second VA does not; after publish the
  second VA's list includes it.
- **Publish** — owner + `calls:publish` → `200`, `published=true`, `published_at` set;
  non-owner → `403`; missing `calls:publish` → `403`; second publish → idempotent no-op with
  no additional `audit_log` row.
- **Join-token gate** — non-owner on a private call → `404`; non-owner on a published call →
  token minted **and** one `call.intervene.join` audit row written.
- **Revoke** — owner → `remove_participant` invoked (gateway mocked) + `call.intervene.revoke`
  audit row; non-owner → `403`; call remains `published`.
- **Audit content** — `call.publish` / `call.intervene.join` / `call.intervene.revoke` rows
  carry ids only, no PHI (no `patient_name`, no transcript).
- **Tenant isolation** — publishing never surfaces a call to another tenant (RLS).
- **One-way** — there is no un-publish endpoint; `published` never transitions back to `False`.
- **Migration** — `published`/`published_at` present, default `False`; existing rows are private.
- **Frontend** (`src/lib/api/calls.test.ts`, mirroring `calls.test.ts` if present) — request
  shapes + error propagation for the four client methods.

## 8. Open follow-ups (not this task)

- **SSE push propagation** — replace/augment polling with a "call published" event over the
  existing transcript SSE pattern for near-instant list updates.
- **Typed intervention actions** — `POST /calls/{id}/interventions` recording
  `InterventionEvent` rows (flag / coach / whisper / takeover), richer LiveKit grants per type,
  form takeover, DTMF. Each carries its own PHI/permission implications.
- **Broader `calls.py` authz cleanup** — move the remaining `calls:read` stopgaps to
  write/manage permissions and add PHI-access audit to `list_calls` (which returns `patient_name`).
- **Ownership handoff / takeover** — if takeover is ever added, define what happens to
  `initiated_by_id` and the owner's revoke rights.
