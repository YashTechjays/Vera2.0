# Auth Audit Log — WORM Hash Chain — Implementation Plan

> **STATUS: DRAFT — one decision pending.** Confirm **Decision D2 (chain scope: per-tenant vs global)** from the design spec before executing. This plan is written for **per-tenant** (recommended); if global is chosen, the only changes are: drop the `tenant_id` partition predicate in the trigger/verifier reads and use a single constant advisory-lock key. Everything else is identical.

> **For agentic workers:** use superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Run the per-task verification before moving on.

**Goal:** Populate a tamper-evident per-row hash chain (`prev_hash`/`row_hash`) on every `auth_audit_log` row, computed in the database across **both** write paths (tenant ORM insert + `log_auth_event` SECURITY DEFINER fn), with **no change** to the Python emit code.

**Architecture:** A single `BEFORE INSERT` trigger is the chokepoint. A new `seq BIGINT IDENTITY` column gives deterministic chain order; a per-chain `pg_advisory_xact_lock` guarantees fork-freedom; one `IMMUTABLE` SQL helper computes the hash for both the trigger and a `verify_auth_audit_chain()` verifier. Existing rows are backfilled in the same migration.

**Tech Stack:** Alembic, Postgres (`pgcrypto`, plpgsql, advisory locks, IDENTITY), SQLAlchemy 2.x async, pytest + pytest-asyncio.

**Scope:** `auth_audit_log` only. The PHI `audit_log` chain, HMAC/anchoring hardening, and any emit-payload changes are out of scope (see spec).

**Source spec:** `docs/superpowers/specs/2026-06-22-auth-audit-hash-chain-design.md`

## Global Constraints

- **`just check` must pass at the end of every task** — `lint` (ruff) + `typecheck` (mypy --strict) + `test` (pytest).
- **DB clock only** — the chain hashes `created_at` (server `now()`); never introduce an app-clock timestamp into the payload.
- **SECURITY DEFINER ownership rule (repo `CLAUDE.md`):** every definer function must be `ALTER FUNCTION ... OWNER TO vera_definer_owner` so BYPASSRLS applies. A later **signature** change needs `DROP FUNCTION` + recreate + re-`ALTER ... OWNER` — `CREATE OR REPLACE` leaves the old overload and loses definer ownership.
- **WORM is sacred** — the trigger only sets columns on the inserting `NEW` row (part of the INSERT, not an UPDATE). Never add an UPDATE/DELETE path to `auth_audit_log` in app code; backfill UPDATEs live only in the migration (superuser, BYPASSRLS).
- **No Python emit change** — `AuthAuditRecord`, `emit_auth_event`, `DatabaseAuthAuditWriter`, and the `log_auth_event` body stay as-is. The only model change is the read-only `seq` column.
- **Migration discipline** — `0001` materializes all tables from `Base.metadata`; this migration only ALTERs/creates functions/trigger/backfill. Confirm it is the new head before applying.

## File Structure

| File | Responsibility | Tasks |
|------|----------------|-------|
| `migrations/versions/00NN_auth_audit_hash_chain.py` | pgcrypto + `seq` column + hash helper + trigger fn + verifier fn + backfill + ownership/grants; full downgrade | 2 |
| `packages/vera_core/src/vera_core/models/auth.py` | add read-only `seq` to `AuthAuditLog` | 1 |
| `tests/integration/control_plane/test_auth_audit_chain.py` | the chain integration tests | 3 |
| `adr/devops-todo.md` | record the tamper-proof (HMAC/anchoring) residual-risk row | 4 |

> **Migration number:** the current head is `0011_platform_login.py`. Confirm with `grep -rln 'down_revision' migrations/versions | xargs grep -l '\"0011\"'` (expect none) — then this migration is `0012`, `down_revision = "0011"`. Re-check at execution time in case another branch landed a `0012`.

---

## Task 1: add the `seq` ordering column to the model

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/auth.py` (`AuthAuditLog`)
- Test: `tests/unit/db/test_models_auth_audit_chain.py` (create)

**Interfaces:**
- Produces: `AuthAuditLog.seq: Mapped[int]` — DB-generated identity, read-only on the ORM (`init=False`), not set by any insert path.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/db/test_models_auth_audit_chain.py`:

```python
from vera_core.models import AuthAuditLog


def test_auth_audit_log_has_seq_identity_column():
    col = AuthAuditLog.__table__.c.seq
    assert col is not None
    assert col.identity is not None  # GENERATED ... AS IDENTITY


def test_hash_columns_present():
    cols = AuthAuditLog.__table__.c
    assert "prev_hash" in cols
    assert "row_hash" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/db/test_models_auth_audit_chain.py -v`
Expected: FAIL — `KeyError: 'seq'`.

- [ ] **Step 3: Add the column**

In `models/auth.py`, on `AuthAuditLog`, add (import `BigInteger` and `Identity` from `sqlalchemy` if not present):

```python
    # Monotonic per-table sequence — the chain's deterministic ordering key.
    # DB-generated; never set by an insert path (created_at is txn-time and the
    # platform path's id is a random UUIDv4, so neither orders the chain).
    seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), nullable=False, init=False
    )
```

> If `AuthAuditLog` is not a dataclass-style mapped model, drop `init=False`. Verify how sibling columns are declared in this file and match. The column must be read-only from the app's perspective regardless.

Update the `AuthAuditLog` docstring to note the `seq` + `prev_hash`/`row_hash` chain is populated by the DB trigger (migration `00NN`), not the app.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/db/test_models_auth_audit_chain.py -v && uv run mypy`
Expected: PASS; mypy clean (136+ files).

- [ ] **Step 5: Commit**

```bash
git add packages/vera_core/src/vera_core/models/auth.py tests/unit/db/test_models_auth_audit_chain.py
git commit -m "feat(models): add seq identity column to auth_audit_log (chain order)"
```

---

## Task 2: migration — pgcrypto, seq, hash helper, trigger, verifier, backfill

**Files:**
- Create: `migrations/versions/0012_auth_audit_hash_chain.py`
- Verified by: `just up && just migrate` applying cleanly + Task 3 integration tests.

**Interfaces:**
- Consumes: `vera_definer_owner` role (migration `0002`), `auth_audit_log` (WORM, `0001`), the `seq` column (Task 1, materialized by `0001`'s `create_all` on a fresh DB).
- Produces: `auth_audit_row_hash(...)` IMMUTABLE helper; `auth_audit_chain()` trigger fn + `BEFORE INSERT` trigger; `verify_auth_audit_chain(uuid)` verifier; every existing row chained; `pgcrypto` enabled.

> **Why one migration:** the column exists on a fresh DB via `0001` create_all; an already-migrated DB needs the `ADD COLUMN`. The trigger/helper/verifier are new objects; the backfill seeds the chain over any pre-existing rows. All idempotent (`IF NOT EXISTS` / `CREATE OR REPLACE`).

- [ ] **Step 1: Write the migration**

Create `migrations/versions/0012_auth_audit_hash_chain.py`. Key pieces (fill in exact SQL; structure shown):

```python
"""auth_audit_log WORM hash chain — pgcrypto, seq, trigger, verifier, backfill

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-22

Per-(tenant_id) SHA-256 hash chain over auth_audit_log. A BEFORE INSERT trigger
computes prev_hash/row_hash for BOTH write paths (tenant ORM insert + the
log_auth_event SECURITY DEFINER fn). `seq` (IDENTITY) is the chain order; a
per-chain advisory xact lock prevents forks. One IMMUTABLE helper computes the
hash for both the trigger and verify_auth_audit_chain(). Existing rows are
backfilled in (created_at, id) order. See spec 2026-06-22-auth-audit-hash-chain.
"""
from collections.abc import Sequence
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"

# --- a) pgcrypto (digest) + seq column (idempotent on already-migrated DBs) ---
# op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
# op.execute("ALTER TABLE auth_audit_log "
#            "ADD COLUMN IF NOT EXISTS seq bigint GENERATED ALWAYS AS IDENTITY")
# op.execute("CREATE INDEX IF NOT EXISTS ix_auth_audit_log_tenant_seq "
#            "ON auth_audit_log (tenant_id, seq)")

# --- b) single source of truth for the hash (D7) ------------------------------
# IMMUTABLE so it can be used in the trigger and verifier identically.
AUTH_AUDIT_ROW_HASH = """
CREATE OR REPLACE FUNCTION auth_audit_row_hash(
    p_prev bytea, p_seq bigint, p_id uuid, p_tenant_id uuid, p_app_user_id uuid,
    p_event_type text, p_ip inet, p_metadata jsonb, p_created_at timestamptz
) RETURNS bytea LANGUAGE sql IMMUTABLE AS $$
    SELECT digest(
        p_prev || convert_to(
            concat_ws('|',
                p_seq::text, p_id::text, coalesce(p_tenant_id::text,''),
                coalesce(p_app_user_id::text,''), p_event_type,
                coalesce(host(p_ip),''), coalesce(p_metadata::text,'{}'),
                to_char(p_created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US')
            ), 'UTF8'),
        'sha256');
$$
"""

# --- c) BEFORE INSERT trigger fn (SECURITY DEFINER → reads past FORCE RLS) -----
# Genesis prev = 32 zero bytes. Advisory lock keyed by tenant (constant for NULL).
AUTH_AUDIT_CHAIN_FN = """
CREATE OR REPLACE FUNCTION auth_audit_chain() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_prev bytea;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended(coalesce(NEW.tenant_id::text,'__platform__'), 0));
    SELECT row_hash INTO v_prev FROM auth_audit_log
     WHERE tenant_id IS NOT DISTINCT FROM NEW.tenant_id
     ORDER BY seq DESC LIMIT 1;
    v_prev := coalesce(v_prev, decode(repeat('00',32),'hex'));
    NEW.prev_hash := v_prev;
    NEW.row_hash := auth_audit_row_hash(
        v_prev, NEW.seq, NEW.id, NEW.tenant_id, NEW.app_user_id,
        NEW.event_type, NEW.ip_address, NEW.metadata, NEW.created_at);
    RETURN NEW;
END $$
"""
# CREATE TRIGGER trg_auth_audit_chain BEFORE INSERT ON auth_audit_log
#   FOR EACH ROW EXECUTE FUNCTION auth_audit_chain();

# --- d) verifier: first broken seq, or NULL if intact (D-verify) --------------
VERIFY_FN = """
CREATE OR REPLACE FUNCTION verify_auth_audit_chain(p_tenant_id uuid)
RETURNS bigint LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
DECLARE r record; v_prev bytea := decode(repeat('00',32),'hex'); v_calc bytea;
BEGIN
    FOR r IN SELECT * FROM auth_audit_log
              WHERE tenant_id IS NOT DISTINCT FROM p_tenant_id ORDER BY seq ASC LOOP
        IF r.prev_hash IS DISTINCT FROM v_prev THEN RETURN r.seq; END IF;
        v_calc := auth_audit_row_hash(v_prev, r.seq, r.id, r.tenant_id,
            r.app_user_id, r.event_type, r.ip_address, r.metadata, r.created_at);
        IF r.row_hash IS DISTINCT FROM v_calc THEN RETURN r.seq; END IF;
        v_prev := r.row_hash;
    END LOOP;
    RETURN NULL;
END $$
"""
```

In `upgrade()`:
1. `pgcrypto` + `ADD COLUMN seq` + index (a).
2. `op.execute` the three functions (b, c, d).
3. Create the `BEFORE INSERT` trigger (c).
4. `GRANT EXECUTE` on `auth_audit_row_hash` is implicit (PUBLIC); `ALTER FUNCTION auth_audit_chain() OWNER TO vera_definer_owner` and `ALTER FUNCTION verify_auth_audit_chain(uuid) OWNER TO vera_definer_owner` (so their reads bypass FORCE RLS). The helper can stay owned by the migration role (it's pure, reads nothing).
5. **Backfill** in one statement per chain using a window function, e.g. recompute `seq` order with a recursive CTE or an ordered `UPDATE ... FROM` that walks rows; simplest robust form: a `DO $$` block looping rows `ORDER BY tenant_id, created_at, id`, carrying `v_prev` per tenant, calling `auth_audit_row_hash`. Runs as superuser → WORM UPDATE allowed.

In `downgrade()`: drop trigger, `verify_auth_audit_chain(uuid)`, `auth_audit_chain()`, `auth_audit_row_hash(...)`; `ALTER TABLE auth_audit_log DROP COLUMN seq`; leave `pgcrypto` (other things may use it) — or drop only if this migration added it. Do **not** null the hashes (table is WORM; downgrade on a dev DB is a full reset anyway).

- [ ] **Step 2: Confirm head + apply**

Run: `grep -rn 'down_revision' migrations/versions/0011_platform_login.py` (expect `revision="0011"`), then `just up && just migrate`.
Expected: applies through `0012`, no error.

- [ ] **Step 3: Manual sanity in psql**

```bash
docker compose exec -T postgres psql -U vera -d vera -c \
  "SELECT seq, length(prev_hash), length(row_hash) FROM auth_audit_log ORDER BY seq;"
docker compose exec -T postgres psql -U vera -d vera -c \
  "SELECT verify_auth_audit_chain(NULL);"   -- expect NULL (intact)
```
Expected: every row has 32-byte prev/row hashes; verifier returns NULL.

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/0012_auth_audit_hash_chain.py
git commit -m "feat(db): WORM hash chain for auth_audit_log (trigger + verifier + backfill)"
```

---

## Task 3: integration tests

**Files:**
- Create: `tests/integration/control_plane/test_auth_audit_chain.py`

**Interfaces:**
- Consumes: live RLS Postgres fixtures (model on `test_platform_login.py` — superuser `sessionmaker` for setup/asserts, `DatabaseAuthAuditWriter` + `log_auth_event` for the two write paths), `verify_auth_audit_chain`.

> Reuse the existing integration DB fixtures (`database_url`, `rls_database_url`). Write auth events through the **real** emit paths so the trigger is exercised, then read back as superuser to assert. Recompute the expected hash in Python (hashlib sha256 over the exact D5 canonical payload) for the linkage test — this independently proves the trigger's formula.

- [ ] **Step 1: Write the tests**

Cover all six (one test each):
1. `test_population_sets_all_hashes_and_seq` — emit N tenant events; every row has non-null 32-byte `prev_hash`/`row_hash`, `seq` strictly increasing.
2. `test_linkage_and_genesis` — `row[n].prev_hash == row[n-1].row_hash`; genesis `prev_hash == b"\x00"*32`; independently recomputed `row_hash` matches the stored one.
3. `test_both_write_paths_chain` — interleave a tenant insert and a platform (`log_auth_event`, `tenant_id=NULL`) insert; assert each chain (tenant vs platform) is independently continuous.
4. `test_tamper_is_detected` — as superuser, `UPDATE` a mid-chain row's `metadata`; `verify_auth_audit_chain(tenant_id)` returns that row's `seq`. (Reset/teardown drops the rows.)
5. `test_concurrent_inserts_do_not_fork` — `asyncio.gather` of N concurrent same-tenant emits (separate sessions); assert the chain is strictly linear: `prev_hash` values are unique and form a single path (no two rows share a predecessor), `verify_...` returns NULL.
6. `test_per_tenant_chains_are_independent` — two tenants + the platform chain each verify independently; a tamper in one doesn't flip another's verifier result.

- [ ] **Step 2: Run**

Run (remapped dev ports):
`VERA_DATABASE_URL=postgresql+asyncpg://vera:vera@localhost:5440/vera VERA_REDIS_URL=redis://localhost:6390/0 LOCAL_KMS_MASTER_KEY=<32B b64> uv run pytest tests/integration/control_plane/test_auth_audit_chain.py -v`
Expected: 6 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/control_plane/test_auth_audit_chain.py
git commit -m "test(audit): auth_audit_log hash-chain integrity, both paths, concurrency, tamper"
```

---

## Task 4: record the residual-risk (tamper-proof) infra obligation

**Files:**
- Modify: `adr/devops-todo.md`

- [ ] **Step 1: Add a row**

Append a row (match the table format): a plain hash chain is **tamper-evident, not tamper-proof** — a `BYPASSRLS` actor can edit a row and recompute all later `row_hash`es. Hardening options: HMAC keyed by a Cloud-KMS secret the DB role can't read, or periodic anchoring of the head hash to an append-only/object-locked external store. Tracks the residual risk; not built in this change. *Source:* auth_audit_log hash chain (2026-06-22).

- [ ] **Step 2: Commit**

```bash
git add adr/devops-todo.md
git commit -m "docs(devops): track tamper-proof hardening for auth_audit_log chain"
```

---

## Task 5: full-suite verification + simplify

- [ ] **Step 1:** `just check` — ruff + mypy --strict + pytest all green (with the dev DB up on the remapped ports).
- [ ] **Step 2:** Run the `/simplify` skill over the diff (quality/altitude cleanup per repo `CLAUDE.md`), then re-run `just check`.
- [ ] **Step 3:** Final commit if simplify changed anything.

---

## Self-Review (to complete during authoring/execution)

**Open decision:** D2 chain scope (per-tenant recommended) — **must be confirmed before Task 2.**

**Verification items to resolve during execution (not assumptions):**
- Task 1: whether `AuthAuditLog` is dataclass-mapped (→ keep/drop `init=False`).
- Task 2: exact migration number (head is `0011` now; re-check for a landed `0012`); whether `pgcrypto` was added by this migration (governs the downgrade drop).
- Task 2: backfill form (recursive CTE vs `DO` loop) — pick whichever passes the Task 3 linkage test deterministically.
- Task 3: confirm the Python recompute byte-matches the SQL `to_char`/`metadata::text` encoding (the most likely source of a false mismatch — pin the format in one place and copy it exactly).

**Type/interface consistency:** `seq` read-only; no change to `AuthAuditRecord` / `emit_auth_event` / `log_auth_event` body; helper signature identical in trigger + verifier.
