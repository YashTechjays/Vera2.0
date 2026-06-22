"""auth_audit_log WORM hash-chain integrity, against live RLS Postgres.

Events are written through the REAL emit paths — `DatabaseAuthAuditWriter` on the
RLS-enforcing connection (tenant path → ORM insert under the tenant GUC; platform
path → the `log_auth_event` SECURITY DEFINER fn) — so the in-DB BEFORE INSERT
trigger (migration 0012) is what populates `seq`/`prev_hash`/`row_hash`. Rows are
read back as superuser (bypasses the WORM SELECT-only RLS) to assert. Each test
uses freshly-minted tenant UUIDs so its tenant chain starts at genesis and is
isolated from every other chain in the shared DB; the platform (NULL-tenant) chain
is shared, so platform-path assertions are made relative to its live tail, never
genesis.
"""

import asyncio
import hashlib
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.audit.writer import AuthAuditRecord, DatabaseAuthAuditWriter
from vera_core.db import uuid7
from vera_core.models import Tenant
from vera_core.models.enums import AuthEvent

GENESIS = b"\x00" * 32

# The D5 canonical payload, rendered field-by-field by Postgres so the Python
# recompute below sources each native rendering (uuid/inet/jsonb/timestamptz) from
# the engine and independently re-runs only the security-relevant step: the
# delimited concat + sha256(prev || payload).
_ROWS_SQL = """
SELECT seq,
       seq::text                                                       AS s_seq,
       id::text                                                        AS s_id,
       coalesce(tenant_id::text, '')                                   AS s_tenant,
       coalesce(app_user_id::text, '')                                 AS s_user,
       event_type                                                      AS s_event,
       coalesce(host(ip_address), '')                                  AS s_ip,
       coalesce(metadata::text, '{}')                                  AS s_meta,
       to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') AS s_ts,
       prev_hash,
       row_hash
  FROM auth_audit_log
 WHERE tenant_id IS NOT DISTINCT FROM :t
 ORDER BY seq ASC
"""


def _recompute(prev: bytes, row: dict[str, Any]) -> bytes:
    payload = "|".join(
        str(row[k])
        for k in ("s_seq", "s_id", "s_tenant", "s_user", "s_event", "s_ip", "s_meta", "s_ts")
    )
    return hashlib.sha256(prev + payload.encode("utf-8")).digest()


async def _chain(
    sm: async_sessionmaker[AsyncSession], tenant_id: UUID | None
) -> list[dict[str, Any]]:
    async with sm() as session:
        result = await session.execute(text(_ROWS_SQL).bindparams(t=tenant_id))
        return [dict(m) for m in result.mappings().all()]


async def _verify(sm: async_sessionmaker[AsyncSession], tenant_id: UUID | None) -> int | None:
    async with sm() as session:
        broken_seq: int | None = await session.scalar(
            text("SELECT verify_auth_audit_chain(:t)").bindparams(t=tenant_id)
        )
        return broken_seq


@pytest.fixture
async def make_tenant(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[Callable[[], Awaitable[UUID]]]:
    """Mint throwaway tenants; delete their auth rows + tenant rows on teardown
    (auth_audit_log is WORM, so the delete only works on the superuser engine)."""
    created: list[UUID] = []

    async def _create() -> UUID:
        tid = uuid7()
        async with admin_sessionmaker() as session, session.begin():
            session.add(Tenant(id=tid, slug=str(tid), name="chain-test", status="active"))
        created.append(tid)
        return tid

    yield _create

    async with admin_sessionmaker() as session, session.begin():
        for tid in created:
            await session.execute(
                text("DELETE FROM auth_audit_log WHERE tenant_id = :t").bindparams(t=tid)
            )
            await session.execute(text("DELETE FROM tenant WHERE id = :t").bindparams(t=tid))


async def _emit_n(writer: DatabaseAuthAuditWriter, tenant_id: UUID | None, n: int) -> None:
    for i in range(n):
        await writer.emit(
            AuthAuditRecord(
                tenant_id=tenant_id,
                event_type=AuthEvent.LOGIN_SUCCESS,
                app_user_id=uuid7(),
                ip_address="10.0.0.1",
                meta={"i": i},
            )
        )


async def _tamper(sm: async_sessionmaker[AsyncSession], tenant_id: UUID, seq: int) -> None:
    """Superuser bypasses WORM — silently edit a mid-chain row's payload. seq is
    per-tenant, so the update is scoped to this tenant's chain."""
    async with sm() as session, session.begin():
        await session.execute(
            text(
                "UPDATE auth_audit_log SET metadata = '{\"tampered\": true}'::jsonb"
                " WHERE tenant_id = :t AND seq = :s"
            ).bindparams(t=tenant_id, s=seq)
        )


async def test_population_sets_all_hashes_and_seq(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid = await make_tenant()
    writer = DatabaseAuthAuditWriter(rls_sessionmaker)
    await _emit_n(writer, tid, 4)

    rows = await _chain(admin_sessionmaker, tid)
    assert len(rows) == 4
    for r in rows:
        assert len(bytes(r["prev_hash"])) == 32
        assert len(bytes(r["row_hash"])) == 32
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(seqs)  # seq ASC == insert order
    assert len(set(seqs)) == len(seqs)  # no duplicate seqs


async def test_linkage_and_genesis(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid = await make_tenant()
    writer = DatabaseAuthAuditWriter(rls_sessionmaker)
    await _emit_n(writer, tid, 3)

    rows = await _chain(admin_sessionmaker, tid)
    assert bytes(rows[0]["prev_hash"]) == GENESIS

    prev = GENESIS
    for r in rows:
        assert bytes(r["prev_hash"]) == prev
        # Independent recompute of sha256(prev || canonical_payload).
        assert bytes(r["row_hash"]) == _recompute(prev, r)
        prev = bytes(r["row_hash"])

    assert await _verify(admin_sessionmaker, tid) is None


async def test_both_write_paths_chain(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid = await make_tenant()
    writer = DatabaseAuthAuditWriter(rls_sessionmaker)

    # Interleave a tenant insert (ORM path) and a platform insert (definer fn).
    platform_before = await _chain(admin_sessionmaker, None)
    await _emit_n(writer, tid, 1)
    await _emit_n(writer, None, 1)
    await _emit_n(writer, tid, 1)
    await _emit_n(writer, None, 1)

    # The tenant chain is continuous on its own (genesis-rooted, fresh tenant).
    assert await _verify(admin_sessionmaker, tid) is None
    tenant_rows = await _chain(admin_sessionmaker, tid)
    assert len(tenant_rows) == 2
    assert bytes(tenant_rows[0]["prev_hash"]) == GENESIS
    assert bytes(tenant_rows[1]["prev_hash"]) == bytes(tenant_rows[0]["row_hash"])

    # The shared platform chain stays intact and grew by exactly the 2 we added,
    # each linking to the prior platform row.
    assert await _verify(admin_sessionmaker, None) is None
    platform_after = await _chain(admin_sessionmaker, None)
    assert len(platform_after) == len(platform_before) + 2
    prev = bytes(platform_before[-1]["row_hash"]) if platform_before else GENESIS
    for r in platform_after[len(platform_before) :]:
        assert bytes(r["prev_hash"]) == prev
        prev = bytes(r["row_hash"])


async def test_tamper_is_detected(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid = await make_tenant()
    writer = DatabaseAuthAuditWriter(rls_sessionmaker)
    await _emit_n(writer, tid, 4)

    rows = await _chain(admin_sessionmaker, tid)
    assert await _verify(admin_sessionmaker, tid) is None  # intact first
    target_seq = rows[1]["seq"]

    await _tamper(admin_sessionmaker, tid, target_seq)

    assert await _verify(admin_sessionmaker, tid) == target_seq


async def test_concurrent_inserts_do_not_fork(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],
) -> None:
    tid = await make_tenant()
    writer = DatabaseAuthAuditWriter(rls_sessionmaker)

    # Each emit opens its own session — fire them concurrently. The per-chain
    # advisory xact lock must serialize them into a single linear chain.
    await asyncio.gather(
        *(
            writer.emit(
                AuthAuditRecord(
                    tenant_id=tid,
                    event_type=AuthEvent.LOGIN_SUCCESS,
                    app_user_id=uuid7(),
                    ip_address="10.0.0.2",
                    meta={"n": n},
                )
            )
            for n in range(8)
        )
    )

    rows = await _chain(admin_sessionmaker, tid)
    assert len(rows) == 8
    # No fork: every prev_hash is distinct and forms one path from genesis.
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
    writer = DatabaseAuthAuditWriter(rls_sessionmaker)
    await _emit_n(writer, tid_a, 3)
    await _emit_n(writer, tid_b, 3)

    assert await _verify(admin_sessionmaker, tid_a) is None
    assert await _verify(admin_sessionmaker, tid_b) is None

    # Tamper tenant A; tenant B and the platform chain stay intact.
    rows_a = await _chain(admin_sessionmaker, tid_a)
    await _tamper(admin_sessionmaker, tid_a, rows_a[1]["seq"])

    assert await _verify(admin_sessionmaker, tid_a) == rows_a[1]["seq"]
    assert await _verify(admin_sessionmaker, tid_b) is None
    assert await _verify(admin_sessionmaker, None) is None
