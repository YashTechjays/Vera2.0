# Call Recording & Persistence — Design

**Date:** 2026-07-09
**Status:** Approved (design review with Yash, 2026-07-09)
**Scope:** vera-backend only (control_plane + agent_worker + vera_core)

## Goal

Record every outbound call as audio in GCS via LiveKit composite egress, persist the
tokenized transcript to Postgres as the single source of truth, enforce a per-tenant
retention policy with audited before/after snapshots, and expose playback through an
RBAC-gated endpoint that mints TTL-bounded signed URLs and logs every access.

## Decisions (settled in design review)

1. **Egress starts from the control plane at room creation** — captures dialing, IVR
   navigation, hold, and the rep conversation. Not from the worker, not on-answer.
2. **Egress-start failure is fail-open**: the call proceeds; the failure is logged and
   audited (`RECORDING_START_FAILED`). The compliance record of record is `audit_log`,
   not the recording; blocking payer calls on egress infra health is the wrong trade.
3. **Transcripts are persisted tokenized, never hydrated.** The Redis transcript stream
   already carries only redacted `[[TYPE_N]]` text; plaintext PHI never sits in Redis;
   the worker has no Postgres access; the session vault is wiped at call end. The
   full-fidelity record is the audio itself (raw voice, CMEK-encrypted, inside the BAA
   boundary). Revisit only as an explicit compliance decision.
4. **Retention policy is per-tenant and tenant-admin-editable**, defaulting from env.
5. **"Before/after snapshots" means both**: deletion events capture object state
   before/after destruction, AND policy changes capture old/new values.
6. **Playback = permission AND call visibility**: a new `recordings:read` permission
   plus the existing call-visibility rule (owner always; otherwise `published` and not
   in `revoked_user_ids`). Recording access is never broader than call access.
7. **Egress completion is detected by a reconciliation poller, not a LiveKit webhook** —
   no new public endpoint, no webhook signature verification, self-healing across
   control-plane restarts; recordings are not latency-sensitive.

## Components

### 1. Egress start (control plane, call-initiation flow)

- `LiveKitGateway.start_room_audio_egress(room_name, gcs_object_path) -> egress_id`
  wrapping `StartRoomCompositeEgress` with `audio_only=true` and GCS file output.
- Called immediately after `create_call_room()`, before the SIP dial.
- Object path: `{prefix}/{tenant_id}/{call_id}.ogg` — opaque UUIDs only, no PHI in
  paths (bright line: PHI never in a URL or path).
- Row creation order: attempt the egress start; on success insert a `Recording` row
  with `status=PENDING`, the returned `egress_id`, and the expected `gcs_uri`; on
  failure insert the row with `status=FAILED` (evidence the recording was attempted)
  and audit `RECORDING_START_FAILED`.
- New `Settings`: `recording_bucket` (`VERA_RECORDING_BUCKET`; unset = recording
  disabled, no-op like Langfuse), `recording_prefix` (`VERA_RECORDING_PREFIX`,
  default `"recordings"`).

### 2. Recording model changes (idempotent migration)

`recording` gains: `status` (CHECK `PENDING | AVAILABLE | FAILED | DISCARDED |
DELETED`), `egress_id`, `sha256`, `size_bytes`, `duration_ms`, `deleted_at`.
Existing columns (`gcs_uri`, `retention_until`) unchanged. Migration follows the
`ADD COLUMN IF NOT EXISTS` + guarded-constraint conventions; revision id via
`just makemigration`.

### 3. Verification poller (control plane, lifespan task)

Every ~30s, for each `PENDING` recording:

- `ListEgress(egress_id)` → still running: skip.
- Complete: stream the GCS object via `asyncio.to_thread` (pattern:
  `vera_core/audit/gcs_anchor.py`), compute **sha256**, record `size_bytes` and
  `duration_ms` (from EgressInfo), flip to `AVAILABLE`, stamp
  `retention_until = call.ended_at + tenant retention days`.
- Egress failed: `status=FAILED` + audit `RECORDING_FAILED`.
- Call ended `NO_ANSWER`/`BUSY`: delete the object, `status=DISCARDED`.
- All transitions idempotent; poller tolerates replicas (row-level state checks).

### 4. Transcript finalizer (worker event → consumer)

- Worker emits new `CallEndedEvent(room_name)` to `vera:worker_events` on session end
  (alongside the existing `CallFailedEvent`; also emitted on failure paths so failed
  calls still persist whatever transcript exists).
- `WorkerEventConsumer` handler replays `vera:transcript:{room_name}` from `0` and
  bulk-inserts `transcript` rows: `seq` = stream order, `role user → REP`,
  `agent → BOT`, tokenized `text` as-is, `spoke_at` from event `ts`.
- Idempotent via `ON CONFLICT (call_id, seq) DO NOTHING` (redelivery-safe; the
  consumer group is at-least-once).
- The existing SSE live view is untouched; Postgres is the source of truth after the
  stream's TTL expires.

### 5. Retention policy + sweeper

- `tenant.recording_retention_days` (nullable int; NULL → settings default
  `VERA_RECORDING_RETENTION_DAYS_DEFAULT`, default 90).
- `PATCH /api/v1/tenant/retention-policy` — gated by new `recordings:manage`
  permission (tenant-admin tier, seeded to TENANT_ADMIN). Audits
  `RETENTION_POLICY_CHANGED` with `{old_days, new_days}`. Applies to recordings
  created after the change (existing `retention_until` stamps are not rewritten).
- Hourly sweeper (lifespan task): for each `AVAILABLE` row with
  `retention_until < now()` (DB clock):
  1. audit **before** snapshot: `{gcs_uri, size_bytes, sha256, retention_until}`;
  2. delete the GCS object (404 = already gone, no-op);
  3. verify the object is gone;
  4. audit **after** confirmation and tombstone the row: `status=DELETED`,
     `deleted_at` (DB clock), `sha256`/`size_bytes` retained as evidence.
- Transcripts are tokenized and are **not** swept — retention governs raw audio only.

### 6. Playback endpoint

`GET /api/v1/calls/{call_id}/recording`

- Requires `recordings:read` **and** call visibility (owner, or `published` and not
  revoked). Platform operators go through the existing elevation model.
- Only `AVAILABLE` recordings are playable: `404` if no row or not visible,
  `409` if `PENDING`/`FAILED`/`DISCARDED`/`DELETED`.
- Response: `{url, expires_at}` — a **V4 signed GCS URL**, TTL
  `VERA_RECORDING_SIGNED_URL_TTL_SECONDS` (default 600). Signed via IAM
  `signBlob` under Workload Identity (no exported key files).
- Every issuance writes `RECORDING_ACCESSED` to `audit_log` (viewer, tenant, call,
  recording id, TTL) — this is the access log, on the append-only trail. Field names
  and ids only, never content.

### 7. New audit events

`RECORDING_START_FAILED`, `RECORDING_FAILED`, `RECORDING_ACCESSED`,
`RECORDING_DELETED` (before/after detail), `RETENTION_POLICY_CHANGED`.
CHECK-widening migration follows the `0017` pattern.

### 8. New permissions

`recordings:read` (seeded to TENANT_ADMIN, SUPERVISOR), `recordings:manage`
(seeded to TENANT_ADMIN). Code + migration per the RBAC catalog convention.

### 9. Infra dependencies (new `adr/devops-todo.md` rows)

- Recordings bucket: CMEK, `roles/storage.objectCreator` for the LiveKit egress
  service account; control-plane SA needs read + delete on the bucket.
- `roles/iam.serviceAccountTokenCreator` on the control-plane SA for V4 signing
  under Workload Identity.
- Bucket lifecycle rule only as a backstop safely **behind** the app-owned sweeper
  (e.g. delete at `max tenant retention + 30d`) so the sweeper always wins and the
  audit trail stays authoritative.

## Error handling summary

| Failure | Behavior |
|---|---|
| Egress start fails | Call proceeds; row inserted with `status=FAILED`; `RECORDING_START_FAILED` audited |
| Egress fails mid-call | Poller marks `FAILED` + audit |
| No-answer / busy call | Object deleted, row `DISCARDED` |
| GCS unreachable in poller/sweeper | Retry next tick; rows stay in current state (self-healing) |
| Transcript event redelivered | `ON CONFLICT DO NOTHING` |
| Sweeper delete races replica | GCS 404 no-op; row update is a state-guarded UPDATE |
| Signed URL requested for non-AVAILABLE | 409; nothing signed, access still audited as deny via authz trail |

## Testing

- Unit: gateway egress call shape, sha256 verifier, sweeper state machine, visibility
  rule, signed-URL TTL clamping.
- Integration: fakes for LiveKit API + GCS; transcript finalizer against a real Redis
  stream (existing test harness in `tests/integration/transcript/`).
- **Boot verification (repo rule):** the verification poller and retention sweeper are
  long-lived loops — verify by booting `just up` + `just api` and watching several
  idle windows (Redis BLOCK/timeout behavior), not pytest alone.
- `just check` green; `/simplify` pass before commit per repo rule.

## Out of scope

- Frontend playback UI.
- Transcript hydration (decision 3).
- Transcript retention/deletion.
- Recording download/export endpoints beyond the signed URL.
- Supervisor live-listen (distinct from playback).
