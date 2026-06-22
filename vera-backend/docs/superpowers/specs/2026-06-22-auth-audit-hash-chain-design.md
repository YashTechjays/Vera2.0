# Auth Audit Log — WORM Hash Chain — Design

**Date:** 2026-06-22
**Status:** Draft — pending sign-off (one open decision: chain scope, see Decision D2)
**Related:** `adr/vera2-database-design.md` (§3.5.9 platform write path, §7 audit/WORM hash chain; ERD `prev_hash`/`row_hash`), ADR-0006 §C (`vera_definer_owner` SECURITY DEFINER write path), `packages/vera_core/src/vera_core/models/auth.py` (`AuthAuditLog`), migrations `0001` (WORM policies) + `0002` (`log_auth_event`).

## Problem

`auth_audit_log` is **already WORM**: migration `0001` registers it in `WORM_TABLES` and gives it SELECT/INSERT-only RLS policies under FORCE ROW LEVEL SECURITY, so UPDATE/DELETE are denied for every non-`BYPASSRLS` connection — owner included. The model carries the `prev_hash` / `row_hash` columns (`models/auth.py:186-187`) for a per-row tamper-evidence chain.

But **nothing populates them.** `AuthAuditRecord` (`audit/writer.py:87-95`) has no hash fields; neither write path sets them:
- Tenant events → ORM `INSERT` under the tenant RLS GUC (`writer.py:128-137`).
- Platform (NULL-tenant) events → the `log_auth_event` SECURITY DEFINER fn (`writer.py:113-127`, migration `0002`).

So every row's `prev_hash`/`row_hash` is NULL: append-only immutability holds, but there is no cryptographic linkage proving the *sequence* hasn't been altered by a privileged (BYPASSRLS) actor. The ADR (`vera2-database-design.md:477`) calls for "a WORM hash chain (`prev_hash`/`row_hash`) to match the PHI `audit_log` discipline." This design adds it.

## Goal & scope

Populate a tamper-evident hash chain on every `auth_audit_log` row, computed at the database, covering **both** write paths, with no change to the Python emit code.

- **In scope:** the `auth_audit_log` chain (trigger + ordering column + verifier + backfill + tests).
- **Out of scope (YAGNI / separate work):**
  - The PHI `audit_log` chain (same columns exist; the approach here transfers, but it's a separate table/migration — not bundled).
  - HMAC/keyed chaining or external anchoring (see "Known limitation" — recorded, not built).
  - Any change to what auth events are emitted or their payloads.

## Key model — one DB trigger as the single chokepoint

Both write paths end in an `INSERT` into `auth_audit_log`. A single `BEFORE INSERT ... FOR EACH ROW` trigger fires on both, so:

- **Zero Python change.** `AuthAuditRecord`, `emit_auth_event`, `DatabaseAuthAuditWriter`, and the `log_auth_event` body are untouched — the hash is computed in-DB, on the DB clock, at one place.
- **No path can skip the chain.** A future third writer (another definer fn, a manual superuser insert) is chained automatically.

Computing the chain in Python instead would duplicate hashing across two paths, be racy (no cross-session serialization), and not use the single clock of record. Rejected.

## Decisions

### D1 — Trigger-based, not Python (DECIDED)
Compute `prev_hash`/`row_hash` in a `BEFORE INSERT` trigger. Rationale above.

### D2 — Chain scope: per-tenant (RECOMMENDED) vs global — **OPEN, needs sign-off**
- **Per-tenant (recommended):** one chain per `tenant_id`; NULL-tenant (platform) events are their own chain. Aligns with RLS — a chain is verifiable within its own tenant scope, and the PHI `audit_log` is already tenant-scoped. Platform events form a clean separate lineage (matching the "tenant_id nullable for platform events" framing in the ADR).
- **Global:** one lineage across all tenants. Conceptually simplest, but reading/verifying it requires crossing every tenant (BYPASSRLS only), so no tenant can verify its own slice.

Everything below assumes **per-tenant**. If global is chosen: drop the chain-partition predicate from the trigger/verifier and use a single advisory-lock key; the rest is unchanged.

### D3 — Chain ordering key: a `seq` IDENTITY column (DECIDED)
A chain needs a deterministic total order. Neither existing column provides one:
- `created_at` defaults to `now()` = **transaction-start** time → ties are possible.
- `id` is UUIDv7 on the tenant path but **`gen_random_uuid()` (UUIDv4, not time-ordered)** on the platform path.

Add `seq BIGINT GENERATED ALWAYS AS IDENTITY`. Monotonic, gap-tolerant, deterministic. The chain orders by `seq`; the verifier walks `seq` ascending.

### D4 — Fork-freedom: per-chain advisory transaction lock (DECIDED)
`seq` gives order but not fork-freedom: two concurrent transactions can't see each other's uncommitted row (MVCC), so both would read the same predecessor and fork the chain. The trigger takes `pg_advisory_xact_lock` keyed by the chain (tenant_id; a constant for the platform chain) **before** reading the last row. Same-chain inserts serialize; different chains don't block; the lock auto-releases at transaction end. Each auth event is already written in its own short transaction (`writer.py`), so lock hold time is minimal.

### D5 — Hashing primitive + canonical serialization (DECIDED)
`row_hash = sha256( prev_hash || convert_to(payload,'UTF8') )` via `pgcrypto`'s `digest(...,'sha256')`. **`pgcrypto` is not yet enabled** (only `vector`, `pg_trgm`) → the migration runs `CREATE EXTENSION IF NOT EXISTS pgcrypto`. `payload` is a fixed-order, delimited encoding of the immutable row fields: `seq | id | tenant_id | app_user_id | event_type | host(ip_address) | metadata::text | created_at(UTC)` (NULLs → empty). `metadata::text` (JSONB) is deterministic within a PG major version; the chain is verified on the same engine that wrote it (documented).

### D6 — Genesis row (DECIDED)
The first row in each chain hashes against a **32 zero-byte** `prev_hash` (stored, not NULL), so verification is uniform (every row has a non-null `prev_hash`).

### D7 — Single source of truth for the hash (DECIDED)
Factor the canonical-payload + `digest` into one `IMMUTABLE` SQL helper `auth_audit_row_hash(...)`. Both the trigger and the verifier call it, so they can never drift.

### D8 — Backfill existing rows (DECIDED)
Existing rows have NULL hashes. The migration backfills them into the chain in `(created_at, id)` order per partition (migrations run as superuser → bypass FORCE RLS / the missing UPDATE policy), so the table is fully chained from row 1 going forward. Pre-prod dev volume is tiny.

## Verification surface

`verify_auth_audit_chain(p_tenant_id uuid) RETURNS bigint` (SECURITY DEFINER, STABLE, owned by `vera_definer_owner`): walk the chain in `seq` order, recompute each `row_hash` via the D7 helper, return the `seq` of the first row whose stored hash ≠ recomputed hash or whose `prev_hash` ≠ the prior `row_hash`; `NULL` if intact. This is the ops/compliance check and the assertion target in tests.

## Known limitation (recorded, not solved here)

A plain SHA-256 chain is **tamper-evident, not tamper-proof.** An attacker who already holds `BYPASSRLS` (the only way to UPDATE/DELETE these rows at all) can edit a row *and recompute every subsequent `row_hash`*, producing an internally-consistent forged chain. Detecting that requires either (a) an **HMAC** keyed by a secret the DB role cannot read (e.g. KMS-held), or (b) periodic **anchoring** of the head hash to an append-only external store (object lock / transparency log). Both are deferred; this row should be added to `adr/devops-todo.md` so the residual risk is tracked, not assumed away. The chain still meaningfully raises the bar (no silent in-place edit) and satisfies the ADR's stated baseline.

## Testing strategy

Live RLS Postgres (mirror `tests/integration/control_plane/test_platform_login.py` fixtures): population (all hashes set, `seq` increasing), linkage + independent recompute, both write paths chaining, tamper detection via `verify_auth_audit_chain`, concurrency/no-fork under `asyncio.gather`, and per-tenant/platform chain isolation. Detailed in the plan.

## Source plan

`docs/superpowers/plans/2026-06-22-auth-audit-hash-chain.md`
