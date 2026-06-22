from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Reuse the make_tenant fixture pattern; import the helper from the chain test module.
from tests.integration.control_plane.test_audit_chain import make_tenant  # noqa: F401
from vera_core.audit.anchor import read_chain_heads
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
                request_id=f"h-{i}",
                detail={"i": i},
            )
        )


async def test_read_chain_heads_returns_latest_per_tenant(
    rls_sessionmaker: async_sessionmaker[AsyncSession],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    make_tenant: Callable[[], Awaitable[UUID]],  # noqa: F811
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
