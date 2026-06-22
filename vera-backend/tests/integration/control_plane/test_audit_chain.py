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
    "s_seq",
    "s_id",
    "s_tenant",
    "s_actor_type",
    "s_actor_user",
    "s_actor_label",
    "s_event",
    "s_resource_type",
    "s_resource_id",
    "s_perm",
    "s_decision",
    "s_request",
    "s_detail",
    "s_reason",
    "s_elev",
    "s_ts",
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
        broken_seq: int | None = await session.scalar(
            text("SELECT verify_audit_chain(:t)").bindparams(t=tenant_id)
        )
        return broken_seq


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
            await session.execute(
                text("DELETE FROM audit_log WHERE tenant_id = :t").bindparams(t=tid)
            )
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
