# PHI-Access Audit Log — WORM Bucket Anchoring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tamper-evident hash chain to the PHI `audit_log`, then periodically anchor each tenant chain's head hash to an object-locked (WORM) GCS bucket so a privileged in-DB rewrite of PHI-access history is externally detectable.

**Architecture:** Phase 1 mirrors migration `0012` (the `auth_audit_log` chain) onto `audit_log` via an in-DB `BEFORE INSERT` trigger — zero Python change to the emit path. Phase 2 adds a pull-based anchoring job: a `SECURITY DEFINER` head-query function, a pluggable `AnchorSink` (`LocalFilesystemAnchorSink` dev / `GCSAnchorSink` prod, selected like `build_kms`), and a verify-against-anchor routine. A GKE CronJob drives cadence; only digests (hashes) leave the DB — no PHI rows are exported.

**Tech Stack:** Python 3.12, SQLAlchemy async, Alembic, PostgreSQL (`pgcrypto`), pydantic-settings, `google-cloud-storage` (lazy, prod only), pytest + pytest-asyncio.

## Global Constraints

- Python pinned `>=3.12,<3.13`. PEP 695 type params only (`class Foo[T]`, `def f[T]`) — ruff rejects `Generic[T]`/`TypeVar`.
- `asyncio` is the only async runtime — never `import anyio`; use `asyncio.to_thread` / `asyncio.TaskGroup`.
- Every audit/anchor timestamp comes from the **DB clock** (`now()` / `func.now()`), never Python `datetime.now()`.
- **No raw PHI** in audit rows or anchor objects — hashes, counts, tokens, entity types only.
- Spec reference: `docs/superpowers/specs/2026-06-22-phi-audit-worm-bucket-anchoring-design.md`.
- DB definer role is `vera_definer_owner` (NOLOGIN BYPASSRLS, created in migration `0002`).
- `audit_log.tenant_id` is NOT NULL (`TenantScopedMixin`) — there is exactly one write path and one chain-partition key (`tenant_id`); no platform/NULL-tenant chain (unlike `auth_audit_log`).
- Verification gate before any "done" claim: `just check` (ruff + mypy --strict + pytest). Run `/simplify` after each task, then re-run `just check`.

---

## Task overview (build in order)

1. **Phase 1** — `audit_log` hash chain: model `seq` column + migration `0013` + integration tests.
2. **Phase 2a** — `AnchorSink` protocol, `LocalFilesystemAnchorSink`, settings, `build_anchor_sink` (unit tests).
3. **Phase 2b** — `audit_chain_heads()` + `audit_row_hash_at()` definer fns: migration `0014` + `read_chain_heads` + integration test.
4. **Phase 2c** — anchor object builder + `run_anchor` command + `verify_against_anchor` (integration tests incl. tamper).
5. **Phase 2d** — `GCSAnchorSink` (lazy `google-cloud-storage`, create-only upload) + dependency + unit test.
6. **Phase 2e** — script entrypoint + justfile recipe + `adr/devops-todo.md` rows.

Detailed tasks follow in subsequent sections of this document (Task 1 below; Tasks 2–6 appended).

---

### Task 1: Phase 1 — `audit_log` hash chain (model + migration `0013` + tests)

**Files:**
- Modify: `packages/vera_core/src/vera_core/models/audit_log.py` (add `seq` column; import `BigInteger`)
- Create: `migrations/versions/0013_audit_log_hash_chain.py`
- Create: `tests/integration/control_plane/test_audit_chain.py`

**Interfaces:**
- Produces (SQL, callable by later tasks/tests):
  - `audit_row_hash(p_prev bytea, p_seq bigint, p_id uuid, p_tenant_id uuid, p_actor_type text, p_actor_user_id uuid, p_actor_label text, p_event_type text, p_resource_type text, p_resource_id text, p_permission_key text, p_decision text, p_request_id text, p_detail jsonb, p_reason text, p_elevation_session_id uuid, p_created_at timestamptz) RETURNS bytea`
  - `verify_audit_chain(p_tenant_id uuid) RETURNS bigint` (NULL = intact; else first broken `seq`)
  - `audit_log.seq bigint NOT NULL` populated by the `audit_chain()` BEFORE INSERT trigger.
- Consumes: `DatabaseAuditWriter` / `AuditRecord` (unchanged, from `vera_core.audit`); fixtures `rls_sessionmaker`, `admin_sessionmaker` (from `tests/integration/control_plane/conftest.py`, same as `test_auth_audit_chain.py`).

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/control_plane/test_audit_chain.py`:

```python
"""audit_log (PHI-access) WORM hash-chain integrity, against live RLS Postgres.

Rows are written through the REAL emit path (DatabaseAuditWriter on the
RLS-enforcing connection → ORM insert under the tenant GUC), so the in-DB
BEFORE INSERT trigger (migration 0013) populates seq/prev_hash/row_hash. Rows
are read back as superuser (bypasses WORM SELECT-only RLS) to assert. Each test
mints fresh tenant UUIDs so its chain starts at genesis and is isolated.
"""

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.audit.writer import AuditRecord, DatabaseAuditWriter
from vera_core.db import uuid7
from vera_core.models import Tenant
from vera_core.models.audit_log import ActorType, AuditEvent

GENESIS = b"\x00" * 32

# Per-field Postgres rendering, mirrored by the Python recompute below so the
# test independently re-runs only sha256(prev || canonical_payload).
_ROWS_SQL = """
SELECT seq,
       seq::text                                                       AS s_seq,
       id::text                                                        AS s_id,
       coalesce(tenant_id::text, '')                                   AS s_tenant,
       actor_type::text                                                AS s_actor_type,
       coalesce(actor_user_id::text, '')                               AS s_actor_user,
       actor_label                                                     AS s_actor_label,
       event_type                                                      AS s_event,
       resource_type                                                   AS s_resource_type,
       resource_id                                                     AS s_resource_id,
       coalesce(permission_key, '')                                    AS s_perm,
       coalesce(decision, '')                                          AS s_decision,
       request_id                                                      AS s_request,
       coalesce(detail::text, '{}')                                    AS s_detail,
       reason                                                          AS s_reason,
       coalesce(elevation_session_id::text, '')                        AS s_elev,
       to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') AS s_ts,
       prev_hash,
       row_hash
  FROM audit_log
 WHERE tenant_id = :t
 ORDER BY seq ASC
"""

_FIELDS = (
    "s_seq", "s_id", "s_tenant", "s_actor_type", "s_actor_user", "s_actor_label",
    "s_event", "s_resource_type", "s_resource_id", "s_perm", "s_decision",
    "s_request", "s_detail", "s_reason", "s_elev", "s_ts",
)


def _recompute(prev: bytes, row: dict[str, Any]) -> bytes:
    payload = "|".join(str(row[k]) for k in _FIELDS)
    return hashlib.sha256(prev + payload.encode("utf-8")).digest()


async def _chain(sm: async_sessionmaker[AsyncSession], tenant_id: UUID) -> list[dict[str, Any]]:
    async with sm() as session:
        result = await session.execute(text(_ROWS_SQL).bindparams(t=tenant_id))
        return [dict(m) for m in result.mappings().all()]


async def _verify(sm: async_sessionmaker[AsyncSession], tenant_id: UUID) -> int | None:
    async with sm() as session:
        return await session.scalar(
            text("SELECT verify_audit_chain(:t)").bindparams(t=tenant_id)
        )


@pytest.fixture
async def make_tenant(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[Callable[[], Awaitable[UUID]]]:
    created: list[UUID] = []

    async def _create() -> UUID:
        tid = uuid7()
        async with admin_sessionmaker() as session, session.begin():
            session.add(Tenant(id=tid, slug=str(tid), name="audit-chain-test", status="active"))
        created.append(tid)
        return tid

    yield _create

    async with admin_sessionmaker() as session, session.begin():
        for tid in created:
            await session.execute(text("DELETE FROM audit_log WHERE tenant_id = :t").bindparams(t=tid))
            await session.execute(text("DELETE FROM tenant WHERE id = :t").bindparams(t=tid))


async def _emit_n(writer: DatabaseAuditWriter, tenant_id: UUID, n: int) -> None:
    for i in range(n):
        await writer.emit(
            AuditRecord(
                tenant_id=tenant_id,
                actor_type=ActorType.USER,
                event_type=AuditEvent.PHI_ACCESS,
                actor_user_id=uuid7(),
                actor_label="tester@example.com",
                resource_type="patient",
                resource_id="ref-123",
                request_id=f"req-{i}",
                detail={"i": i},
            )
        )


async def _tamper(sm: async_sessionmaker[AsyncSession], tenant_id: UUID, seq: int) -> None:
    async with sm() as session, session.begin():
        await session.execute(
            text(
                "UPDATE audit_log SET detail = '{\"tampered\": true}'::jsonb"
                " WHERE tenant_id = :t AND seq = :s"
            ).bindparams(t=tenant_id, s=seq)
        )


async def test_population_sets_all_hashes_and_seq(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid = await make_tenant()
    await _emit_n(DatabaseAuditWriter(rls_sessionmaker), tid, 4)
    rows = await _chain(admin_sessionmaker, tid)
    assert len(rows) == 4
    for r in rows:
        assert len(bytes(r["prev_hash"])) == 32
        assert len(bytes(r["row_hash"])) == 32
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


async def test_linkage_and_genesis(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid = await make_tenant()
    await _emit_n(DatabaseAuditWriter(rls_sessionmaker), tid, 3)
    rows = await _chain(admin_sessionmaker, tid)
    assert bytes(rows[0]["prev_hash"]) == GENESIS
    prev = GENESIS
    for r in rows:
        assert bytes(r["prev_hash"]) == prev
        assert bytes(r["row_hash"]) == _recompute(prev, r)
        prev = bytes(r["row_hash"])
    assert await _verify(admin_sessionmaker, tid) is None


async def test_tamper_is_detected(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid = await make_tenant()
    await _emit_n(DatabaseAuditWriter(rls_sessionmaker), tid, 4)
    rows = await _chain(admin_sessionmaker, tid)
    assert await _verify(admin_sessionmaker, tid) is None
    target = rows[1]["seq"]
    await _tamper(admin_sessionmaker, tid, target)
    assert await _verify(admin_sessionmaker, tid) == target


async def test_concurrent_inserts_do_not_fork(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid = await make_tenant()
    writer = DatabaseAuditWriter(rls_sessionmaker)
    await asyncio.gather(
        *(
            writer.emit(
                AuditRecord(
                    tenant_id=tid,
                    actor_type=ActorType.USER,
                    event_type=AuditEvent.PHI_ACCESS,
                    actor_user_id=uuid7(),
                    request_id=f"c-{n}",
                    detail={"n": n},
                )
            )
            for n in range(8)
        )
    )
    rows = await _chain(admin_sessionmaker, tid)
    assert len(rows) == 8
    prevs = [bytes(r["prev_hash"]) for r in rows]
    assert len(set(prevs)) == len(prevs)
    prev = GENESIS
    for r in rows:
        assert bytes(r["prev_hash"]) == prev
        prev = bytes(r["row_hash"])
    assert await _verify(admin_sessionmaker, tid) is None


async def test_per_tenant_chains_are_independent(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid_a = await make_tenant()
    tid_b = await make_tenant()
    writer = DatabaseAuditWriter(rls_sessionmaker)
    await _emit_n(writer, tid_a, 3)
    await _emit_n(writer, tid_b, 3)
    assert await _verify(admin_sessionmaker, tid_a) is None
    assert await _verify(admin_sessionmaker, tid_b) is None
    rows_a = await _chain(admin_sessionmaker, tid_a)
    await _tamper(admin_sessionmaker, tid_a, rows_a[1]["seq"])
    assert await _verify(admin_sessionmaker, tid_a) == rows_a[1]["seq"]
    assert await _verify(admin_sessionmaker, tid_b) is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `just up && just migrate && uv run --package control_plane pytest tests/integration/control_plane/test_audit_chain.py -v`
Expected: FAIL — `verify_audit_chain` does not exist / `column "seq" does not exist`.

- [ ] **Step 3: Add the `seq` column to the model**

In `packages/vera_core/src/vera_core/models/audit_log.py`, add `BigInteger` to the SQLAlchemy import line and add the column after `row_hash` (line ~70):

```python
from sqlalchemy import BigInteger, Enum, ForeignKey, LargeBinary, String, Text
```
```python
    # Chain ordering key, populated by the audit_chain() BEFORE INSERT trigger
    # (migration 0013). Not an IDENTITY: assigned inside the per-tenant advisory
    # lock so seq order == commit order and the chain cannot fork.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
```

- [ ] **Step 4: Write migration `0013`**

Create `migrations/versions/0013_audit_log_hash_chain.py` (mirrors `0012`; per-tenant only, no platform chain). Use the canonical payload field order matching `_FIELDS` in the test:

```python
"""audit_log WORM hash chain — seq, trigger, verifier, backfill

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-22

Per-(tenant_id) SHA-256 hash chain over the PHI-access audit_log, mirroring the
auth_audit_log chain (migration 0012). A BEFORE INSERT trigger assigns seq and
links prev_hash/row_hash inside a per-tenant advisory xact lock so the chain
cannot fork. One IMMUTABLE helper computes the hash for both the trigger and
verify_audit_chain(). audit_log.tenant_id is NOT NULL, so there is a single
write path and a single chain partition key (no platform chain).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_SEARCH_PATH = "SET search_path = pg_catalog, public"

AUDIT_ROW_HASH = """
CREATE OR REPLACE FUNCTION audit_row_hash(
    p_prev bytea, p_seq bigint, p_id uuid, p_tenant_id uuid,
    p_actor_type text, p_actor_user_id uuid, p_actor_label text,
    p_event_type text, p_resource_type text, p_resource_id text,
    p_permission_key text, p_decision text, p_request_id text,
    p_detail jsonb, p_reason text, p_elevation_session_id uuid,
    p_created_at timestamptz
) RETURNS bytea
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT digest(
        p_prev || convert_to(
            concat_ws('|',
                p_seq::text, p_id::text, coalesce(p_tenant_id::text, ''),
                p_actor_type, coalesce(p_actor_user_id::text, ''), p_actor_label,
                p_event_type, p_resource_type, p_resource_id,
                coalesce(p_permission_key, ''), coalesce(p_decision, ''),
                p_request_id, coalesce(p_detail::text, '{}'), p_reason,
                coalesce(p_elevation_session_id::text, ''),
                to_char(p_created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US')
            ), 'UTF8'),
        'sha256');
$$
"""

AUDIT_CHAIN_FN = f"""
CREATE OR REPLACE FUNCTION audit_chain() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    v_seq bigint;
    v_prev bytea;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text, 0));
    SELECT seq, row_hash INTO v_seq, v_prev
      FROM audit_log
     WHERE tenant_id = NEW.tenant_id
     ORDER BY seq DESC
     LIMIT 1;
    NEW.seq := coalesce(v_seq, 0) + 1;
    NEW.prev_hash := coalesce(v_prev, decode(repeat('00', 32), 'hex'));
    NEW.row_hash := audit_row_hash(
        NEW.prev_hash, NEW.seq, NEW.id, NEW.tenant_id, NEW.actor_type::text,
        NEW.actor_user_id, NEW.actor_label, NEW.event_type, NEW.resource_type,
        NEW.resource_id, NEW.permission_key, NEW.decision, NEW.request_id,
        NEW.detail, NEW.reason, NEW.elevation_session_id, NEW.created_at);
    RETURN NEW;
END;
$$
"""

VERIFY_FN = f"""
CREATE OR REPLACE FUNCTION verify_audit_chain(p_tenant_id uuid)
RETURNS bigint
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
DECLARE
    r record;
    v_prev bytea := decode(repeat('00', 32), 'hex');
    v_calc bytea;
BEGIN
    FOR r IN
        SELECT * FROM audit_log
         WHERE tenant_id = p_tenant_id
         ORDER BY seq ASC
    LOOP
        IF r.prev_hash IS DISTINCT FROM v_prev THEN
            RETURN r.seq;
        END IF;
        v_calc := audit_row_hash(
            v_prev, r.seq, r.id, r.tenant_id, r.actor_type::text,
            r.actor_user_id, r.actor_label, r.event_type, r.resource_type,
            r.resource_id, r.permission_key, r.decision, r.request_id,
            r.detail, r.reason, r.elevation_session_id, r.created_at);
        IF r.row_hash IS DISTINCT FROM v_calc THEN
            RETURN r.seq;
        END IF;
        v_prev := r.row_hash;
    END LOOP;
    RETURN NULL;
END;
$$
"""

BACKFILL = """
DO $$
DECLARE
    r record;
    v_zero bytea := decode(repeat('00', 32), 'hex');
    v_prev bytea;
    v_hash bytea;
    v_seq bigint;
    v_cur_tenant uuid;
    v_started boolean := false;
BEGIN
    FOR r IN
        SELECT * FROM audit_log
         ORDER BY tenant_id, created_at, id
    LOOP
        IF NOT v_started OR r.tenant_id IS DISTINCT FROM v_cur_tenant THEN
            v_prev := v_zero;
            v_seq := 0;
            v_cur_tenant := r.tenant_id;
            v_started := true;
        END IF;
        v_seq := v_seq + 1;
        v_hash := audit_row_hash(
            v_prev, v_seq, r.id, r.tenant_id, r.actor_type::text,
            r.actor_user_id, r.actor_label, r.event_type, r.resource_type,
            r.resource_id, r.permission_key, r.decision, r.request_id,
            r.detail, r.reason, r.elevation_session_id, r.created_at);
        UPDATE audit_log SET seq = v_seq, prev_hash = v_prev, row_hash = v_hash
         WHERE id = r.id;
        v_prev := v_hash;
    END LOOP;
END $$
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS seq bigint")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_log_tenant_seq ON audit_log (tenant_id, seq)"
    )
    op.execute(f"GRANT SELECT ON audit_log TO {DEFINER_ROLE}")
    op.execute(AUDIT_ROW_HASH)
    op.execute(AUDIT_CHAIN_FN)
    op.execute(VERIFY_FN)
    op.execute(
        "CREATE TRIGGER trg_audit_chain BEFORE INSERT ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION audit_chain()"
    )
    op.execute(f"ALTER FUNCTION audit_chain() OWNER TO {DEFINER_ROLE}")
    op.execute(f"ALTER FUNCTION verify_audit_chain(uuid) OWNER TO {DEFINER_ROLE}")
    op.execute(BACKFILL)
    op.execute("ALTER TABLE audit_log ALTER COLUMN seq SET NOT NULL")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_chain ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS verify_audit_chain(uuid)")
    op.execute("DROP FUNCTION IF EXISTS audit_chain()")
    op.execute(
        "DROP FUNCTION IF EXISTS audit_row_hash("
        "bytea, bigint, uuid, uuid, text, uuid, text, text, text, text, text, text,"
        " text, jsonb, text, uuid, timestamptz)"
    )
    op.execute(f"REVOKE SELECT ON audit_log FROM {DEFINER_ROLE}")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_tenant_seq")
    op.execute("ALTER TABLE audit_log DROP COLUMN IF EXISTS seq")
```

- [ ] **Step 5: Apply the migration and run the tests**

Run: `just migrate && uv run --package control_plane pytest tests/integration/control_plane/test_audit_chain.py -v`
Expected: PASS (all 5 tests).

- [ ] **Step 6: Verify, simplify, full gate**

Run: `just check` → expect green. Then run the `/simplify` skill on the changed files, then re-run `just check`.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/models/audit_log.py migrations/versions/0013_audit_log_hash_chain.py tests/integration/control_plane/test_audit_chain.py
git commit -m "feat(audit): add WORM hash chain to PHI audit_log (mirror 0012)"
```

---

### Task 2: Phase 2a — `AnchorSink` protocol, `LocalFilesystemAnchorSink`, settings, `build_anchor_sink`

**Files:**
- Create: `packages/vera_core/src/vera_core/audit/anchor.py`
- Modify: `packages/vera_core/src/vera_core/audit/__init__.py` (export the new symbols)
- Modify: `packages/vera_core/src/vera_core/config/settings.py` (add 3 fields)
- Create: `tests/unit/audit/test_anchor_sink.py`

**Interfaces:**
- Consumes: `Settings` (from `vera_core.config.settings`).
- Produces:
  - `class AnchorSink(Protocol)` with `async def write_anchor(self, key: str, body: bytes) -> None` and `async def read_latest(self) -> bytes | None`
  - `class LocalFilesystemAnchorSink` (ctor `(root: Path)`)
  - `def build_anchor_sink(settings: Settings) -> AnchorSink`
  - `Settings.audit_anchor_bucket: str | None`, `Settings.audit_anchor_prefix: str`, `Settings.audit_anchor_local_dir: str`

- [ ] **Step 1: Write the failing unit test**

Create `tests/unit/audit/test_anchor_sink.py`:

```python
from pathlib import Path

import pytest

from vera_core.audit.anchor import (
    AnchorSink,
    LocalFilesystemAnchorSink,
    build_anchor_sink,
)
from vera_core.config.settings import Settings


async def test_local_sink_write_then_read_latest(tmp_path: Path) -> None:
    sink = LocalFilesystemAnchorSink(tmp_path)
    assert await sink.read_latest() is None
    await sink.write_anchor("anchors/2026/06/22/2026-06-22T00:00:00.000000-a.json", b"first")
    await sink.write_anchor("anchors/2026/06/22/2026-06-22T01:00:00.000000-b.json", b"second")
    assert await sink.read_latest() == b"second"  # lexicographically last key wins


async def test_local_sink_is_create_only(tmp_path: Path) -> None:
    sink = LocalFilesystemAnchorSink(tmp_path)
    await sink.write_anchor("anchors/x.json", b"one")
    with pytest.raises(FileExistsError):
        await sink.write_anchor("anchors/x.json", b"two")  # WORM: no overwrite


def test_build_anchor_sink_selects_local_when_no_bucket(tmp_path: Path) -> None:
    settings = Settings(audit_anchor_bucket=None, audit_anchor_local_dir=str(tmp_path))
    sink = build_anchor_sink(settings)
    assert isinstance(sink, LocalFilesystemAnchorSink)


def test_anchor_sink_protocol_is_runtime_checkable(tmp_path: Path) -> None:
    assert isinstance(LocalFilesystemAnchorSink(tmp_path), AnchorSink)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package vera-core pytest tests/unit/audit/test_anchor_sink.py -v`
Expected: FAIL — `ModuleNotFoundError: vera_core.audit.anchor`.

- [ ] **Step 3: Add settings fields**

In `packages/vera_core/src/vera_core/config/settings.py`, after the observability block (line ~80), add:

```python
    # --- audit anchoring (WORM bucket) -------------------------------------
    # Periodic anchoring of audit_log chain heads to an object-locked GCS bucket
    # (tamper-PROOF hardening of the tamper-EVIDENT hash chain; devops-todo #10b).
    # Set audit_anchor_bucket → GCSAnchorSink (prod); unset → LocalFilesystemAnchorSink (dev).
    audit_anchor_bucket: str | None = None
    audit_anchor_prefix: str = "audit-anchors"
    audit_anchor_local_dir: str = ".audit-anchors"
```

- [ ] **Step 4: Write the sink module (protocol + local sink + builder)**

Create `packages/vera_core/src/vera_core/audit/anchor.py`:

```python
"""WORM anchoring of audit_log chain heads to an object-locked external store.

Only DIGESTS leave the DB — per-tenant chain heads (hashes + counts), never PHI
rows. The job is pull-based (run by a CronJob); see the GCSAnchorSink for prod
and LocalFilesystemAnchorSink for dev/test. build_anchor_sink mirrors build_kms.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from vera_core.config.settings import Settings


@runtime_checkable
class AnchorSink(Protocol):
    async def write_anchor(self, key: str, body: bytes) -> None:
        """Create an immutable anchor object at `key`. MUST NOT overwrite."""
        ...

    async def read_latest(self) -> bytes | None:
        """Return the body of the most recent anchor object, or None if none."""
        ...


class LocalFilesystemAnchorSink:
    """Dev / test sink. NOT a compliance store. Create-only (no overwrite)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    async def write_anchor(self, key: str, body: bytes) -> None:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise FileExistsError(key)
        path.write_bytes(body)

    async def read_latest(self) -> bytes | None:
        files = sorted(self._root.rglob("*.json"))
        return files[-1].read_bytes() if files else None


def build_anchor_sink(settings: "Settings") -> AnchorSink:
    if settings.audit_anchor_bucket:
        from vera_core.audit.gcs_anchor import GCSAnchorSink  # lazy: prod only

        return GCSAnchorSink(settings.audit_anchor_bucket, settings.audit_anchor_prefix)
    return LocalFilesystemAnchorSink(Path(settings.audit_anchor_local_dir))
```

- [ ] **Step 5: Export the symbols**

In `packages/vera_core/src/vera_core/audit/__init__.py`, add to the import block and `__all__`:

```python
from .anchor import AnchorSink, LocalFilesystemAnchorSink, build_anchor_sink
```
Add `"AnchorSink"`, `"LocalFilesystemAnchorSink"`, `"build_anchor_sink"` to `__all__`.

- [ ] **Step 6: Run tests + gate**

Run: `uv run --package vera-core pytest tests/unit/audit/test_anchor_sink.py -v` → PASS.
Run `just check`, then `/simplify`, then `just check` again.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/audit/anchor.py packages/vera_core/src/vera_core/audit/__init__.py packages/vera_core/src/vera_core/config/settings.py tests/unit/audit/test_anchor_sink.py
git commit -m "feat(audit): add AnchorSink protocol, local sink, and settings"
```

---

### Task 3: Phase 2b — `audit_chain_heads()` + `audit_row_hash_at()` definer fns (migration `0014`) + `read_chain_heads`

**Files:**
- Create: `migrations/versions/0014_audit_chain_heads.py`
- Modify: `packages/vera_core/src/vera_core/audit/anchor.py` (add `ChainHead` + `read_chain_heads`)
- Create: `tests/integration/control_plane/test_audit_chain_heads.py`

**Interfaces:**
- Consumes: `verify_audit_chain` and the chain from Task 1; `DatabaseAuditWriter`/`AuditRecord`; fixtures from `test_audit_chain.py` style.
- Produces:
  - SQL `audit_chain_heads() RETURNS TABLE(tenant_id uuid, head_seq bigint, head_row_hash bytea, row_count bigint)` (SECURITY DEFINER, STABLE)
  - SQL `audit_row_hash_at(p_tenant_id uuid, p_seq bigint) RETURNS bytea` (SECURITY DEFINER, STABLE) — returns the stored `row_hash` at a (tenant, seq), or NULL.
  - `@dataclass(frozen=True) class ChainHead { tenant_id: UUID; head_seq: int; head_row_hash: bytes; row_count: int }`
  - `async def read_chain_heads(sm: async_sessionmaker[AsyncSession]) -> list[ChainHead]`

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/control_plane/test_audit_chain_heads.py`:

```python
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.audit.anchor import read_chain_heads
from vera_core.audit.writer import AuditRecord, DatabaseAuditWriter
from vera_core.db import uuid7
from vera_core.models.audit_log import ActorType, AuditEvent

# Reuse the make_tenant fixture pattern; import the helper from the chain test module.
from tests.integration.control_plane.test_audit_chain import make_tenant  # noqa: F401


async def _emit_n(writer: DatabaseAuditWriter, tid: UUID, n: int) -> None:
    for i in range(n):
        await writer.emit(
            AuditRecord(
                tenant_id=tid,
                actor_type=ActorType.USER,
                event_type=AuditEvent.PHI_ACCESS,
                actor_user_id=uuid7(),
                request_id=f"h-{i}",
                detail={"i": i},
            )
        )


async def test_read_chain_heads_returns_latest_per_tenant(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid_a = await make_tenant()
    tid_b = await make_tenant()
    writer = DatabaseAuditWriter(rls_sessionmaker)
    await _emit_n(writer, tid_a, 3)
    await _emit_n(writer, tid_b, 5)

    heads = {h.tenant_id: h for h in await read_chain_heads(admin_sessionmaker)}
    assert heads[tid_a].head_seq == 3
    assert heads[tid_a].row_count == 3
    assert len(heads[tid_a].head_row_hash) == 32
    assert heads[tid_b].head_seq == 5
    assert heads[tid_b].row_count == 5
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --package control_plane pytest tests/integration/control_plane/test_audit_chain_heads.py -v`
Expected: FAIL — `function audit_chain_heads() does not exist` (and `read_chain_heads` import error).

- [ ] **Step 3: Write migration `0014`**

Create `migrations/versions/0014_audit_chain_heads.py`:

```python
"""audit_chain_heads + audit_row_hash_at — definer read helpers for anchoring

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-22

Two SECURITY DEFINER read helpers (owned by vera_definer_owner, BYPASSRLS) used
by the WORM anchoring job: audit_chain_heads() returns the latest row per tenant
chain (seq, row_hash, count); audit_row_hash_at() returns the stored row_hash at
a (tenant, seq) so verify-against-anchor can compare an externally anchored head
to current DB state across tenants. EXECUTE defaults to PUBLIC (like the verify
fns), so the app role can call them without a cross-tenant RLS grant.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFINER_ROLE = "vera_definer_owner"
_SEARCH_PATH = "SET search_path = pg_catalog, public"

CHAIN_HEADS_FN = f"""
CREATE OR REPLACE FUNCTION audit_chain_heads()
RETURNS TABLE(tenant_id uuid, head_seq bigint, head_row_hash bytea, row_count bigint)
LANGUAGE sql
STABLE
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
    SELECT DISTINCT ON (a.tenant_id)
           a.tenant_id,
           a.seq AS head_seq,
           a.row_hash AS head_row_hash,
           count(*) OVER (PARTITION BY a.tenant_id) AS row_count
      FROM audit_log a
     ORDER BY a.tenant_id, a.seq DESC;
$$
"""

ROW_HASH_AT_FN = f"""
CREATE OR REPLACE FUNCTION audit_row_hash_at(p_tenant_id uuid, p_seq bigint)
RETURNS bytea
LANGUAGE sql
STABLE
SECURITY DEFINER
{_SEARCH_PATH}
AS $$
    SELECT row_hash FROM audit_log
     WHERE tenant_id = p_tenant_id AND seq = p_seq;
$$
"""


def upgrade() -> None:
    op.execute(CHAIN_HEADS_FN)
    op.execute(ROW_HASH_AT_FN)
    op.execute(f"ALTER FUNCTION audit_chain_heads() OWNER TO {DEFINER_ROLE}")
    op.execute(f"ALTER FUNCTION audit_row_hash_at(uuid, bigint) OWNER TO {DEFINER_ROLE}")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS audit_chain_heads()")
    op.execute("DROP FUNCTION IF EXISTS audit_row_hash_at(uuid, bigint)")
```

- [ ] **Step 4: Add `ChainHead` + `read_chain_heads` to `anchor.py`**

Append to `packages/vera_core/src/vera_core/audit/anchor.py` (and add the imports `from dataclasses import dataclass`, `from uuid import UUID`, `from sqlalchemy import text`, `from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker` at the top):

```python
@dataclass(frozen=True)
class ChainHead:
    tenant_id: UUID
    head_seq: int
    head_row_hash: bytes
    row_count: int


async def read_chain_heads(sm: async_sessionmaker[AsyncSession]) -> list[ChainHead]:
    async with sm() as session:
        rows = await session.execute(
            text(
                "SELECT tenant_id, head_seq, head_row_hash, row_count"
                " FROM audit_chain_heads()"
            )
        )
        return [
            ChainHead(
                tenant_id=r.tenant_id,
                head_seq=r.head_seq,
                head_row_hash=bytes(r.head_row_hash),
                row_count=r.row_count,
            )
            for r in rows
        ]
```
Export `ChainHead` and `read_chain_heads` in `audit/__init__.py`.

- [ ] **Step 5: Apply migration + run test + gate**

Run: `just migrate && uv run --package control_plane pytest tests/integration/control_plane/test_audit_chain_heads.py -v` → PASS.
Run `just check`, then `/simplify`, then `just check`.

- [ ] **Step 6: Commit**

```bash
git add migrations/versions/0014_audit_chain_heads.py packages/vera_core/src/vera_core/audit/anchor.py packages/vera_core/src/vera_core/audit/__init__.py tests/integration/control_plane/test_audit_chain_heads.py
git commit -m "feat(audit): add chain-head + row-hash-at definer fns and read_chain_heads"
```

---

### Task 4: Phase 2c — anchor object builder + `run_anchor` + `verify_against_anchor`

**Files:**
- Modify: `packages/vera_core/src/vera_core/audit/anchor.py`
- Create: `tests/unit/audit/test_anchor_object.py`
- Create: `tests/integration/control_plane/test_anchor_run.py`

**Interfaces:**
- Consumes: `ChainHead`, `read_chain_heads`, `AnchorSink` (Tasks 2–3); SQL `verify_audit_chain`, `audit_row_hash_at` (Tasks 1, 3).
- Produces:
  - `GENESIS_ANCHOR = b"\x00" * 32`
  - `def build_anchor_object(heads, prev_anchor_sha256, run_id, anchored_at) -> tuple[dict, bytes]` — returns `(obj, serialized_body)`; `obj["anchor_sha256"]` is sha256 hex of the canonical core.
  - `def anchor_key(anchored_at: str, run_id: UUID) -> str` — `anchors/{YYYY}/{MM}/{DD}/{anchored_at}-{run_id}.json`
  - `async def run_anchor(sm, sink) -> str` — reads heads + prior anchor, builds + writes one immutable object, returns its key.
  - `async def verify_against_anchor(sm, anchor_obj: dict) -> list[dict]` — returns mismatches (`reason` ∈ {`chain_broken`, `head_mismatch`}).

- [ ] **Step 1: Write the failing unit test (pure builder)**

Create `tests/unit/audit/test_anchor_object.py`:

```python
import hashlib
import json
from uuid import UUID

from vera_core.audit.anchor import (
    GENESIS_ANCHOR,
    ChainHead,
    anchor_key,
    build_anchor_object,
)


def _head(seq: int) -> ChainHead:
    return ChainHead(
        tenant_id=UUID("11111111-1111-1111-1111-111111111111"),
        head_seq=seq,
        head_row_hash=b"\xab" * 32,
        row_count=seq,
    )


def test_build_anchor_object_is_deterministic_and_self_hashing() -> None:
    run_id = UUID("22222222-2222-2222-2222-222222222222")
    obj, body = build_anchor_object([_head(3)], GENESIS_ANCHOR, run_id, "2026-06-22T00:00:00.000000")
    assert obj["run_id"] == str(run_id)
    assert obj["prev_anchor_sha256"] == GENESIS_ANCHOR.hex()
    assert obj["chains"][0]["head_row_hash"] == ("ab" * 32)
    # anchor_sha256 = sha256 over the canonical core (everything except anchor_sha256)
    core = {k: v for k, v in obj.items() if k != "anchor_sha256"}
    expected = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert obj["anchor_sha256"] == expected
    assert json.loads(body) == obj


def test_anchor_key_partitions_by_date() -> None:
    run_id = UUID("22222222-2222-2222-2222-222222222222")
    key = anchor_key("2026-06-22T01:02:03.000000", run_id)
    assert key == f"anchors/2026/06/22/2026-06-22T01:02:03.000000-{run_id}.json"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --package vera-core pytest tests/unit/audit/test_anchor_object.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_anchor_object'`.

- [ ] **Step 3: Implement the builder + key + command + verify**

Append to `packages/vera_core/src/vera_core/audit/anchor.py` (add imports `import hashlib`, `import json`, `from uuid import UUID, uuid4`):

```python
GENESIS_ANCHOR = b"\x00" * 32


def _serialize(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_anchor_object(
    heads: list[ChainHead],
    prev_anchor_sha256: bytes,
    run_id: UUID,
    anchored_at: str,
) -> tuple[dict, bytes]:
    core = {
        "run_id": str(run_id),
        "anchored_at": anchored_at,
        "prev_anchor_sha256": prev_anchor_sha256.hex(),
        "chains": sorted(
            (
                {
                    "tenant_id": str(h.tenant_id),
                    "head_seq": h.head_seq,
                    "head_row_hash": h.head_row_hash.hex(),
                    "row_count": h.row_count,
                }
                for h in heads
            ),
            key=lambda c: c["tenant_id"],
        ),
    }
    anchor_sha = hashlib.sha256(_serialize(core)).hexdigest()
    obj = {**core, "anchor_sha256": anchor_sha}
    return obj, _serialize(obj)


def anchor_key(anchored_at: str, run_id: UUID) -> str:
    y, m, d = anchored_at[:10].split("-")
    return f"anchors/{y}/{m}/{d}/{anchored_at}-{run_id}.json"


async def _db_now_utc(session: AsyncSession) -> str:
    return await session.scalar(  # type: ignore[return-value]
        text("SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US')")
    )


async def run_anchor(sm: async_sessionmaker[AsyncSession], sink: AnchorSink) -> str:
    heads = await read_chain_heads(sm)
    prev_body = await sink.read_latest()
    prev_sha = (
        GENESIS_ANCHOR
        if prev_body is None
        else bytes.fromhex(json.loads(prev_body)["anchor_sha256"])
    )
    run_id = uuid4()
    async with sm() as session:
        anchored_at = await _db_now_utc(session)
    _obj, body = build_anchor_object(heads, prev_sha, run_id, anchored_at)
    key = anchor_key(anchored_at, run_id)
    await sink.write_anchor(key, body)
    return key


async def verify_against_anchor(
    sm: async_sessionmaker[AsyncSession], anchor_obj: dict
) -> list[dict]:
    mismatches: list[dict] = []
    async with sm() as session:
        for chain in anchor_obj["chains"]:
            tid = UUID(chain["tenant_id"])
            broken = await session.scalar(
                text("SELECT verify_audit_chain(:t)").bindparams(t=tid)
            )
            if broken is not None:
                mismatches.append(
                    {"tenant_id": chain["tenant_id"], "reason": "chain_broken", "seq": broken}
                )
                continue
            row_hash = await session.scalar(
                text("SELECT audit_row_hash_at(:t, :s)").bindparams(
                    t=tid, s=chain["head_seq"]
                )
            )
            if row_hash is None or bytes(row_hash).hex() != chain["head_row_hash"]:
                mismatches.append(
                    {
                        "tenant_id": chain["tenant_id"],
                        "reason": "head_mismatch",
                        "seq": chain["head_seq"],
                    }
                )
    return mismatches
```
Export `build_anchor_object`, `anchor_key`, `run_anchor`, `verify_against_anchor`, `GENESIS_ANCHOR` in `audit/__init__.py`.

- [ ] **Step 4: Run the unit test**

Run: `uv run --package vera-core pytest tests/unit/audit/test_anchor_object.py -v` → PASS.

- [ ] **Step 5: Write the failing integration test (end-to-end + tamper)**

Create `tests/integration/control_plane/test_anchor_run.py`:

```python
import json
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.audit.anchor import (
    LocalFilesystemAnchorSink,
    run_anchor,
    verify_against_anchor,
)
from vera_core.audit.writer import AuditRecord, DatabaseAuditWriter
from vera_core.db import uuid7
from vera_core.models.audit_log import ActorType, AuditEvent

from tests.integration.control_plane.test_audit_chain import make_tenant  # noqa: F401


async def _emit_n(writer: DatabaseAuditWriter, tid: UUID, n: int) -> None:
    for i in range(n):
        await writer.emit(
            AuditRecord(
                tenant_id=tid,
                actor_type=ActorType.USER,
                event_type=AuditEvent.PHI_ACCESS,
                actor_user_id=uuid7(),
                request_id=f"a-{i}",
                detail={"i": i},
            )
        )


async def test_run_anchor_then_verify_intact(
    tmp_path,
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid = await make_tenant()
    await _emit_n(DatabaseAuditWriter(rls_sessionmaker), tid, 3)
    sink = LocalFilesystemAnchorSink(tmp_path)

    key = await run_anchor(admin_sessionmaker, sink)
    body = (tmp_path / key).read_bytes()
    obj = json.loads(body)
    assert any(c["tenant_id"] == str(tid) and c["head_seq"] == 3 for c in obj["chains"])

    assert await verify_against_anchor(admin_sessionmaker, obj) == []


async def test_anchor_detects_privileged_rewrite(
    tmp_path,
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid = await make_tenant()
    await _emit_n(DatabaseAuditWriter(rls_sessionmaker), tid, 3)
    sink = LocalFilesystemAnchorSink(tmp_path)
    key = await run_anchor(admin_sessionmaker, sink)
    obj = json.loads((tmp_path / key).read_bytes())

    # A BYPASSRLS actor edits a row AND recomputes the whole chain so the in-DB
    # chain is self-consistent (verify_audit_chain passes) — but the head no
    # longer matches the externally anchored head.
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(
            text("UPDATE audit_log SET detail = '{\"x\":1}'::jsonb WHERE tenant_id=:t AND seq=2")
            .bindparams(t=tid)
        )
        # Re-chain rows seq>=2 via the same helper so internal verify still passes.
        await s.execute(
            text(
                """
                DO $$
                DECLARE r record; v_prev bytea; v_hash bytea;
                BEGIN
                    SELECT row_hash INTO v_prev FROM audit_log
                     WHERE tenant_id = :t AND seq = 1;
                    FOR r IN SELECT * FROM audit_log WHERE tenant_id = :t AND seq >= 2 ORDER BY seq LOOP
                        v_hash := audit_row_hash(v_prev, r.seq, r.id, r.tenant_id,
                            r.actor_type::text, r.actor_user_id, r.actor_label, r.event_type,
                            r.resource_type, r.resource_id, r.permission_key, r.decision,
                            r.request_id, r.detail, r.reason, r.elevation_session_id, r.created_at);
                        UPDATE audit_log SET prev_hash = v_prev, row_hash = v_hash WHERE id = r.id;
                        v_prev := v_hash;
                    END LOOP;
                END $$;
                """.replace(":t", f"'{tid}'::uuid")
            )
        )

    mismatches = await verify_against_anchor(admin_sessionmaker, obj)
    assert any(m["reason"] == "head_mismatch" and m["tenant_id"] == str(tid) for m in mismatches)
```

- [ ] **Step 6: Run integration tests + gate**

Run: `just migrate && uv run --package control_plane pytest tests/integration/control_plane/test_anchor_run.py -v` → PASS (both).
Run `just check`, then `/simplify`, then `just check`.

- [ ] **Step 7: Commit**

```bash
git add packages/vera_core/src/vera_core/audit/anchor.py packages/vera_core/src/vera_core/audit/__init__.py tests/unit/audit/test_anchor_object.py tests/integration/control_plane/test_anchor_run.py
git commit -m "feat(audit): build + write anchor objects and verify against them"
```

---

### Task 5: Phase 2d — `GCSAnchorSink` (lazy `google-cloud-storage`, create-only)

**Files:**
- Create: `packages/vera_core/src/vera_core/audit/gcs_anchor.py`
- Modify: `apps/control_plane/pyproject.toml` (add `google-cloud-storage` dependency)
- Create: `tests/unit/audit/test_gcs_anchor.py`

**Interfaces:**
- Consumes: `AnchorSink` protocol (Task 2). Imported lazily by `build_anchor_sink`.
- Produces: `class GCSAnchorSink` (ctor `(bucket: str, prefix: str)`), satisfying `AnchorSink`. Uploads with `if_generation_match=0` (create-only). `read_latest` lists by prefix and picks the lexicographically-greatest object name.

- [ ] **Step 1: Write the failing unit test (mocked SDK)**

Create `tests/unit/audit/test_gcs_anchor.py`:

```python
import sys
import types
from unittest.mock import MagicMock

import pytest

from vera_core.audit.anchor import AnchorSink


@pytest.fixture
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Inject a fake google.cloud.storage module so the lazy import resolves."""
    storage = MagicMock(name="storage")
    google = types.ModuleType("google")
    google_cloud = types.ModuleType("google.cloud")
    google_cloud.storage = storage  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage)
    return storage


async def test_write_anchor_is_create_only(fake_storage: MagicMock) -> None:
    from vera_core.audit.gcs_anchor import GCSAnchorSink

    blob = fake_storage.Client.return_value.bucket.return_value.blob.return_value
    sink = GCSAnchorSink("my-bucket", "audit-anchors")
    await sink.write_anchor("anchors/2026/06/22/x.json", b"body")

    fake_storage.Client.return_value.bucket.assert_called_once_with("my-bucket")
    fake_storage.Client.return_value.bucket.return_value.blob.assert_called_once_with(
        "audit-anchors/anchors/2026/06/22/x.json"
    )
    blob.upload_from_string.assert_called_once()
    assert blob.upload_from_string.call_args.kwargs["if_generation_match"] == 0


def test_gcs_sink_satisfies_protocol(fake_storage: MagicMock) -> None:
    from vera_core.audit.gcs_anchor import GCSAnchorSink

    assert isinstance(GCSAnchorSink("b", "p"), AnchorSink)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --package vera-core pytest tests/unit/audit/test_gcs_anchor.py -v`
Expected: FAIL — `ModuleNotFoundError: vera_core.audit.gcs_anchor`.

- [ ] **Step 3: Implement `GCSAnchorSink`**

Create `packages/vera_core/src/vera_core/audit/gcs_anchor.py`:

```python
"""Production AnchorSink: writes immutable anchor objects to an object-locked
GCS bucket. google-cloud-storage is a sync SDK, so every call is wrapped in
asyncio.to_thread (the stack is asyncio-locked; no anyio). Uploads are
create-only (if_generation_match=0); the bucket's locked retention policy is the
real WORM guarantee (provisioned per adr/devops-todo.md)."""

import asyncio


class GCSAnchorSink:
    def __init__(self, bucket: str, prefix: str) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    async def write_anchor(self, key: str, body: bytes) -> None:
        await asyncio.to_thread(self._write_sync, key, body)

    def _write_sync(self, key: str, body: bytes) -> None:
        from google.cloud import storage  # lazy: prod-only dependency

        blob = storage.Client().bucket(self._bucket).blob(self._full_key(key))
        blob.upload_from_string(
            body, content_type="application/json", if_generation_match=0
        )

    async def read_latest(self) -> bytes | None:
        return await asyncio.to_thread(self._read_latest_sync)

    def _read_latest_sync(self) -> bytes | None:
        from google.cloud import storage  # lazy

        client = storage.Client()
        prefix = self._full_key("anchors/")
        blobs = list(client.list_blobs(self._bucket, prefix=prefix))
        if not blobs:
            return None
        return max(blobs, key=lambda b: b.name).download_as_bytes()
```

- [ ] **Step 4: Add the dependency**

In `apps/control_plane/pyproject.toml`, add to the `dependencies` list (alongside `google-cloud-kms`):

```toml
    "google-cloud-storage>=2.18",
```
Then sync: `uv sync`.

- [ ] **Step 5: Run tests + gate**

Run: `uv run --package vera-core pytest tests/unit/audit/test_gcs_anchor.py -v` → PASS.
Run `just check`, then `/simplify`, then `just check`.

- [ ] **Step 6: Commit**

```bash
git add packages/vera_core/src/vera_core/audit/gcs_anchor.py apps/control_plane/pyproject.toml uv.lock tests/unit/audit/test_gcs_anchor.py
git commit -m "feat(audit): add GCSAnchorSink (create-only, object-locked bucket)"
```

---

### Task 6: Phase 2e — script entrypoint + justfile recipe + devops-todo rows

**Files:**
- Create: `scripts/audit_anchor.py`
- Modify: `justfile` (add `anchor-audit` recipe)
- Modify: `adr/devops-todo.md` (add bucket-provisioning row; cross-reference #10)

**Interfaces:**
- Consumes: `build_anchor_sink`, `run_anchor` (Tasks 2, 4); `get_settings`; the engine/sessionmaker setup pattern from `scripts/seed.py`.
- Produces: a runnable CronJob entrypoint `python scripts/audit_anchor.py` printing the written anchor key.

- [ ] **Step 1: Read the existing engine-setup pattern**

Run: `sed -n '1,40p' scripts/seed.py`
Use the same `create_engine` / `create_sessionmaker` construction it uses (so this script matches repo conventions exactly).

- [ ] **Step 2: Write the entrypoint script**

Create `scripts/audit_anchor.py` (adapt the engine/sessionmaker lines to match `scripts/seed.py`):

```python
"""CronJob entrypoint: anchor audit_log chain heads to the WORM bucket.

Reads each tenant chain's head via audit_chain_heads(), writes one immutable
anchor object (digests only — no PHI) to the configured sink (GCS in prod,
local filesystem in dev). Schedule/cadence is owned by the GKE CronJob, not this
script (default hourly; see the spec)."""

import asyncio

from vera_core.audit.anchor import build_anchor_sink, run_anchor
from vera_core.config.settings import get_settings
from vera_core.db import create_engine, create_sessionmaker


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    try:
        sink = build_anchor_sink(settings)
        key = await run_anchor(sessionmaker, sink)
        print(f"anchored: {key}")  # noqa: T201 — CLI output
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 3: Add the justfile recipe**

In `justfile`, after the `seed-schemas` recipe, add:

```make
# Anchor audit_log chain heads to the WORM bucket (GKE CronJob entrypoint)
anchor-audit:
    uv run python scripts/audit_anchor.py
```

- [ ] **Step 4: Smoke-test the entrypoint locally**

Run: `just up && just migrate && just anchor-audit`
Expected: prints `anchored: anchors/<YYYY>/<MM>/<DD>/...json`, and the file exists under `.audit-anchors/` (the dev `audit_anchor_local_dir`). Run twice — the second run prints a new key (new timestamp/run_id) and does not error (no overwrite).

- [ ] **Step 5: Add the infra obligations to `adr/devops-todo.md`**

Append a new row (#11) to the table and update #10's status note to reference it:

```markdown
| 11 | ☐ **Provision an object-locked GCS audit-anchor bucket + least-privilege SA.** Create a bucket with a **locked retention policy** (irreversible; sized to the HIPAA minimum-retention requirement), **uniform bucket-level access**, **CMEK** encryption, and object versioning. Grant the control-plane CronJob's Workload Identity SA `roles/storage.objectCreator` on this bucket **only** (no delete/admin). Set `VERA_AUDIT_ANCHOR_BUCKET` (and optional `VERA_AUDIT_ANCHOR_PREFIX`) in the control-plane deployment; unset → `LocalFilesystemAnchorSink` (dev only). Schedule the `anchor-audit` entrypoint as a GKE CronJob (default hourly). | The audit_log hash chain (migration 0013) is tamper-EVIDENT only; anchoring each chain head to a WORM bucket makes a privileged in-DB rewrite externally detectable (closes devops-todo #10 option (b) for the PHI audit log). The bucket must be immutable and the job write-only so the anchor history cannot be edited or deleted. | PHI-audit WORM anchoring (2026-06-22); spec `docs/superpowers/specs/2026-06-22-phi-audit-worm-bucket-anchoring-design.md`. |
```

- [ ] **Step 6: Gate + commit**

Run `just check`, then `/simplify`, then `just check`.
```bash
git add scripts/audit_anchor.py justfile adr/devops-todo.md
git commit -m "feat(audit): add anchor-audit CronJob entrypoint + infra obligations"
```

---

## Self-Review

**Spec coverage:**
- Phase 1 chain (trigger/seq/verifier/backfill) → Task 1. ✓
- `AnchorSink` + `Local`/`GCS` sinks + `build_anchor_sink` (mirrors `build_kms`) → Tasks 2, 5. ✓
- `audit_chain_heads()` SECURITY DEFINER head query → Task 3. ✓
- Anchor object (run_id, anchored_at on DB clock, prev_anchor_sha256 chaining, per-tenant heads, anchor_sha256), immutable timestamped key → Task 4. ✓
- Verify-against-anchor (verify_audit_chain + head-hash compare via `audit_row_hash_at`) → Tasks 3–4. ✓
- Digests-only / zero PHI egress → enforced by anchoring only hashes/counts (Tasks 3–4). ✓
- Cadence via CronJob, configurable default hourly → Task 6 (recipe + devops-todo row). ✓
- Infra obligations (locked retention, CMEK, objectCreator-only) → Task 6. ✓
- Out of scope (full export, HMAC, anchoring auth_audit_log) → not implemented, as specified. ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code and exact commands. (Task 6 Step 1 directs reading `scripts/seed.py` to match the exact engine constructor signature — concrete pointer, not a placeholder.)

**Type consistency:** `ChainHead` fields (`tenant_id`, `head_seq`, `head_row_hash`, `row_count`) are consistent across Tasks 3–4 and the SQL `audit_chain_heads()` columns. `AnchorSink` methods (`write_anchor`, `read_latest`) match across `LocalFilesystemAnchorSink` (Task 2) and `GCSAnchorSink` (Task 5). The `audit_row_hash(...)` signature is identical in the trigger, verifier, backfill (Task 1) and the tamper test (Task 4). `anchor_key`/`build_anchor_object`/`run_anchor`/`verify_against_anchor` signatures match between definitions (Task 4) and tests.

