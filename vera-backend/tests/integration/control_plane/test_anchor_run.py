import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.control_plane.test_audit_chain import make_tenant  # noqa: F401
from vera_core.audit.anchor import (
    LocalFilesystemAnchorSink,
    run_anchor,
    verify_against_anchor,
)
from vera_core.audit.writer import AuditRecord, DatabaseAuditWriter
from vera_core.db import uuid7
from vera_core.models.audit_log import ActorType, AuditEvent


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
    tmp_path: Path,
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],  # noqa: F811
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
    tmp_path: Path,
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],  # noqa: F811
) -> None:
    tid = await make_tenant()
    await _emit_n(DatabaseAuditWriter(rls_sessionmaker), tid, 3)
    sink = LocalFilesystemAnchorSink(tmp_path)
    key = await run_anchor(admin_sessionmaker, sink)
    obj = json.loads((tmp_path / key).read_bytes())

    # A BYPASSRLS actor edits a row AND recomputes the whole chain so the in-DB
    # chain is self-consistent (verify_audit_chain passes) — but the head no
    # longer matches the externally anchored head.
    tid_lit = f"'{tid}'::uuid"
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(
            text(
                "UPDATE audit_log SET detail = cast(:d as jsonb) WHERE tenant_id=:t AND seq=2"
            ).bindparams(d='{"x":1}', t=tid)
        )
        # Re-chain rows seq>=2 via the same helper so internal verify still passes.
        do_sql = f"""
                DO $$
                DECLARE r record; v_prev bytea; v_hash bytea;
                BEGIN
                    SELECT row_hash INTO v_prev FROM audit_log
                     WHERE tenant_id = {tid_lit} AND seq = 1;
                    FOR r IN SELECT * FROM audit_log
                     WHERE tenant_id = {tid_lit} AND seq >= 2 ORDER BY seq LOOP
                        v_hash := audit_row_hash(v_prev, r.seq, r.id, r.tenant_id,
                            r.actor_type::text, r.actor_user_id, r.actor_label, r.event_type,
                            r.resource_type, r.resource_id, r.permission_key, r.decision,
                            r.request_id, r.detail, r.reason, r.elevation_session_id, r.created_at);
                        UPDATE audit_log SET prev_hash = v_prev, row_hash = v_hash WHERE id = r.id;
                        v_prev := v_hash;
                    END LOOP;
                END $$;
                """
        await s.execute(text(do_sql))

    mismatches = await verify_against_anchor(admin_sessionmaker, obj)
    assert any(m["reason"] == "head_mismatch" and m["tenant_id"] == str(tid) for m in mismatches)
