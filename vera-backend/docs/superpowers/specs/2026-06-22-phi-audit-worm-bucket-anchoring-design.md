# PHI-Access Audit Log — WORM Bucket Anchoring — Design

**Date:** 2026-06-22
**Status:** Draft — pending sign-off
**Related:**
- `adr/devops-todo.md` #10 (harden the audit hash chain tamper-EVIDENT → tamper-PROOF; option (b) = anchor head hash to an object-locked external store) and #7 (GCS bucket + retention/CMEK obligations).
- `docs/superpowers/specs/2026-06-22-auth-audit-hash-chain-design.md` (sibling chain on `auth_audit_log`; this design transfers that approach to `audit_log` and adds the external anchor it deferred).
- `packages/vera_core/src/vera_core/models/audit_log.py` (`AuditLog`, `prev_hash`/`row_hash` columns), `packages/vera_core/src/vera_core/audit/writer.py` (`DatabaseAuditWriter`), migrations `0001` (WORM policies), `0012` (the `auth_audit_log` chain we mirror).
- `vera-backend/CLAUDE.md` + `packages/vera_core/src/vera_core/CLAUDE.md` (audit = HIPAA evidence trail; DB clock of record; "smaller boundary is a safer boundary"; no raw PHI in audit rows).

## Problem

The PHI-access audit trail (`audit_log`) is **already WORM**: migration `0001` lists it in `WORM_TABLES` with SELECT/INSERT-only RLS under FORCE ROW LEVEL SECURITY, so UPDATE/DELETE are denied for every non-`BYPASSRLS` connection — owner included. This satisfies the HIPAA §164.312(b) baseline of an immutable audit log.

Two gaps remain:

1. **No tamper-evidence chain on `audit_log`.** The model carries `prev_hash`/`row_hash` (`models/audit_log.py:67-70`) but *nothing populates them* — `DatabaseAuditWriter` (`writer.py:58-76`) never sets them, and there is no trigger. Unlike `auth_audit_log` (chained in migration `0012`), every PHI-access row's hash columns are NULL. There is no cryptographic linkage proving the *sequence* of PHI accesses hasn't been altered.

2. **No external anchor.** Even once a chain exists, a plain SHA-256 chain is tamper-**evident**, not tamper-**proof**: a `BYPASSRLS` actor (the only role that can mutate WORM rows at all) can edit a row and recompute every later `row_hash`, leaving a self-consistent forged chain. `adr/devops-todo.md` #10 names the fix: periodically **anchor the head hash to an append-only / object-locked external store** so any rewrite becomes externally detectable.

This design closes both, specifically for the PHI-access `audit_log`.

## Goal & scope

Produce a tamper-evident hash chain on `audit_log` and periodically anchor each chain's head to an **object-locked (WORM) GCS bucket**, so a privileged in-DB rewrite of PHI-access history is externally detectable.

- **In scope:**
  - **Phase 1 — `audit_log` hash chain:** seq column, `BEFORE INSERT` chain trigger, `verify_audit_chain()` verifier, backfill, tests (mirrors migration `0012`).
  - **Phase 2 — WORM anchoring:** a pluggable `AnchorSink` (`GCSAnchorSink` for prod, `LocalFilesystemAnchorSink` for dev/test), a `SECURITY DEFINER` head-query function, an anchoring command, and a verify-against-anchor routine. A GKE CronJob drives cadence.
- **Out of scope (YAGNI / separate work):**
  - **Full record export** of `audit_log` rows to the bucket (a disaster-recovery / independent-archive concern; DB backups cover plain durability). This design anchors *digests only* — no PHI-bearing rows leave the DB.
  - **HMAC/keyed chaining** (devops-todo #10 option (a)) — complementary, separately tracked.
  - Anchoring `auth_audit_log` (its chain exists; the same `AnchorSink` can later anchor it — wiring is a trivial follow-up, not bundled here).
  - Any change to what PHI-access events are emitted or their payloads.

## Why this approach (vs alternatives considered)

**Anchoring mechanism — pull-based scheduled job (CHOSEN)** over:
- *Anchor-on-write* (push the new head to GCS on every audit insert): puts a network write on the hot PHI-access path, coupling request latency and availability to GCS. Rejected.
- *DB trigger → external callout*: couples the DB transaction to network I/O and violates the repo's "DB stays the local clock/compute of record" discipline. Rejected.

The pull-based job is decoupled, cheap (one small object per run), tunable (detection window = cadence), and reuses the existing pluggable-sink pattern (`AuditSink`/`build_kms`). Its only cost is that rewrites within one un-anchored interval are not *externally* caught — acceptable, and the residual is the HMAC option tracked separately.

**Digests-only, not full export:** the task is tamper-detection, not DR. Anchoring only hashes keeps **zero PHI egress** from the DB trust boundary, matching "a smaller boundary is a safer boundary."

## Phase 1 — `audit_log` hash chain

Mirrors migration `0012` (`auth_audit_log`) with two simplifications specific to `audit_log`:

- **Per-tenant chains only — no platform chain.** `AuditRecord.tenant_id` is non-optional and `audit_log` has *no* `SECURITY DEFINER`/NULL-tenant write path (confirmed: only `auth_audit_log` got `log_auth_event` in migration `0002`). Every row is written by `DatabaseAuditWriter` under a tenant RLS GUC. So there is exactly one write path and one chain partition key (`tenant_id`).
- **One DB trigger as the single chokepoint.** A `BEFORE INSERT ... FOR EACH ROW` trigger computes `seq`/`prev_hash`/`row_hash` in-DB on the DB clock. **Zero Python change** to `AuditRecord`/`DatabaseAuditWriter`; any future writer is chained automatically.

Components (all in one Alembic migration):
- `CREATE EXTENSION IF NOT EXISTS pgcrypto` (for `digest`).
- `seq BIGINT` ordering column + `ix_audit_log_tenant_seq (tenant_id, seq)`.
- `audit_row_hash(...)` — one `IMMUTABLE` SQL helper, single source of truth for the canonical payload + hash (so trigger and verifier can't drift). `row_hash = sha256( prev_hash || convert_to(payload,'UTF8') )`. Canonical payload is a fixed-order, delimited encoding of the immutable row fields: `seq | id | tenant_id | actor_type | actor_user_id | actor_label | event_type | resource_type | resource_id | permission_key | decision | request_id | detail::text | reason | elevation_session_id | created_at(UTC µs)` (NULLs → empty string). `detail::text` (JSONB) is deterministic within a PG major version; the chain is verified on the same engine that wrote it (documented).
- `audit_chain()` trigger fn — `SECURITY DEFINER`, fixed `search_path`, owned by `vera_definer_owner`. Takes `pg_advisory_xact_lock` keyed by `tenant_id` before reading the chain tail, then sets `seq = tail.seq + 1`, `prev_hash = tail.row_hash` (genesis = 32 zero bytes, stored not NULL), `row_hash = audit_row_hash(...)`. The advisory lock serializes same-tenant inserts so the chain can never fork (same rationale as `0012`).
- `GRANT SELECT ON audit_log TO vera_definer_owner` so the trigger reads the tail past FORCE RLS.
- `verify_audit_chain(p_tenant_id uuid) RETURNS bigint` — `SECURITY DEFINER`, `STABLE`: walks one chain in `seq` order, recomputes each `row_hash`, returns the `seq` of the first divergent row or NULL if intact.
- **Backfill** existing rows into the chain in `(created_at, id)` order per tenant (migration runs as superuser → may UPDATE the WORM rows here, and only here), then `seq SET NOT NULL`.

## Phase 2 — WORM anchoring

**Head-query function.** `audit_chain_heads() RETURNS TABLE(tenant_id uuid, head_seq bigint, head_row_hash bytea, row_count bigint)` — `SECURITY DEFINER`, `STABLE`, owned by `vera_definer_owner` (BYPASSRLS) so it reads the latest row per tenant across all chains in one call.

**`AnchorSink` protocol** (mirrors `AuditSink`):
```
class AnchorSink(Protocol):
    async def write_anchor(self, key: str, body: bytes) -> None: ...
```
- `GCSAnchorSink(bucket, prefix)` — prod. Wraps `google-cloud-storage` (a sync SDK) in `asyncio.to_thread` to honor the asyncio-locked stack (no `anyio`). Uploads with `if_generation_match=0` (create-only; never overwrite).
- `LocalFilesystemAnchorSink(root)` — dev/test. Writes under a local dir. NOT a compliance store.
- `build_anchor_sink(settings)` selects GCS vs local by `VERA_AUDIT_ANCHOR_BUCKET` (set → GCS; unset → local), exactly like `build_kms`.

**Anchor object (digests only — no PHI).** One JSON object per run:
```
{
  "run_id": "<uuid>",
  "anchored_at": "<DB now() UTC>",
  "prev_anchor_sha256": "<hash of the previous anchor object, or 32 zero bytes>",
  "chains": [ { "tenant_id", "head_seq", "head_row_hash", "row_count" }, ... ],
  "anchor_sha256": "<sha256 over the canonical-serialized fields above>"
}
```
Anchors form their own append-only chain via `prev_anchor_sha256`, so deletion or reordering of anchor objects is also detectable, not just row edits.

**Object key (immutable, unique per run):** `{prefix}/anchors/{YYYY}/{MM}/{DD}/{anchored_at}-{run_id}.json`. Combined with bucket retention-lock + create-only upload, no run can overwrite a prior anchor.

**Anchoring command.** A control-plane CLI/entrypoint (`just anchor-audit` / `python -m control_plane.audit_anchor`): read `audit_chain_heads()`, read the last anchor (for `prev_anchor_sha256`), build + serialize + hash the object, `write_anchor(...)`. Runs in its own short transaction; `anchored_at` comes from the DB clock. Idempotent w.r.t. a run_id.

**Verify-against-anchor routine.** Given an anchor object and the live DB: for each chain, (a) run `verify_audit_chain(tenant_id)` (catches internal divergence); (b) re-read the row at `head_seq` and confirm its `row_hash == anchor.head_row_hash` — a privileged rewrite that re-self-consistented the chain still mismatches the externally-anchored head. This is the ops/compliance check and a test assertion target.

**Cadence.** Driven by the GKE CronJob schedule (configurable; **default hourly**). Detection window = cadence. The app code is cadence-agnostic — no in-app scheduler.

## Infra obligations (to add to `adr/devops-todo.md`)

Code cannot enforce these; they must be provisioned and verified:
- **Provision an object-locked GCS audit-anchor bucket:** a **retention policy** sized to the HIPAA minimum-retention requirement, with the **retention policy locked** (irreversible — even project owners cannot shorten/delete before expiry); **uniform bucket-level access**; **CMEK** encryption; object versioning on.
- **Least privilege:** grant the control-plane (CronJob) Workload Identity SA `roles/storage.objectCreator` on this bucket **only** — no `objectAdmin`, no delete. The job creates anchors; it can never remove them.
- **Config:** set `VERA_AUDIT_ANCHOR_BUCKET` (and optional `VERA_AUDIT_ANCHOR_PREFIX`) in the control-plane deployment. Unset → `LocalFilesystemAnchorSink` (dev only).
- Note this closes `devops-todo` #10 option (b) for `audit_log`; #10 option (a) (HMAC) remains open and complementary.

## Verification surface

- `verify_audit_chain(tenant_id)` — in-DB chain integrity (Phase 1).
- `audit_chain_heads()` — current per-tenant heads (Phase 2 input).
- verify-against-anchor routine — external tamper detection (Phase 2): combines `verify_audit_chain` with a head-hash comparison against a chosen anchor object.

## Known limitation (recorded, not solved here)

Anchoring is **tamper-detecting, not tamper-preventing**, and only at **cadence granularity**: a row written *and* rewritten within a single un-anchored interval is not externally caught. Closing that fully requires an HMAC keyed by a secret the DB role cannot read (devops-todo #10 option (a)), which is deferred and complementary. This design still removes the "silent privileged rewrite of history" risk for anything that survived one anchor cycle, which is the gap #10 names.

## Testing strategy

Live RLS Postgres (mirror `tests/integration/control_plane/test_auth_audit_chain.py` fixtures):
- **Phase 1:** population (all hashes set, `seq` increasing per tenant), linkage + independent recompute, tamper detection via `verify_audit_chain`, concurrency/no-fork under `asyncio.gather`, per-tenant chain isolation, backfill correctness.
- **Phase 2:** anchoring command against `LocalFilesystemAnchorSink` — object content matches `audit_chain_heads()`, key uniqueness/immutability, `prev_anchor_sha256` linkage across runs; the verify-against-anchor routine returns intact for an honest chain and **detects** a `BYPASSRLS` row rewrite + full chain recompute (self-consistent in-DB, but head mismatches the prior anchor); `build_anchor_sink` selection by env.

## Source plan

`docs/superpowers/plans/2026-06-22-phi-audit-worm-bucket-anchoring.md` (to be written next via the writing-plans skill).
