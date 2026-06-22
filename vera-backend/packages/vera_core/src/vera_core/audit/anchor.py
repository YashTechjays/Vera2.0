"""WORM anchoring of audit_log chain heads to an object-locked external store.

Only DIGESTS leave the DB — per-tenant chain heads (hashes + counts), never PHI
rows. The job is pull-based (run by a CronJob); see the GCSAnchorSink for prod
and LocalFilesystemAnchorSink for dev/test. build_anchor_sink mirrors build_kms.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


@dataclass(frozen=True)
class ChainHead:
    tenant_id: UUID
    head_seq: int
    head_row_hash: bytes
    row_count: int


async def read_chain_heads(sm: async_sessionmaker[AsyncSession]) -> list[ChainHead]:
    async with sm() as session:
        rows = await session.execute(
            text("SELECT tenant_id, head_seq, head_row_hash, row_count FROM audit_chain_heads()")
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


def build_anchor_sink(settings: "Settings") -> AnchorSink:
    if settings.audit_anchor_bucket:
        from vera_core.audit.gcs_anchor import (  # type: ignore[import-not-found]
            GCSAnchorSink,  # lazy: prod only
        )

        return GCSAnchorSink(settings.audit_anchor_bucket, settings.audit_anchor_prefix)  # type: ignore[no-any-return]
    return LocalFilesystemAnchorSink(Path(settings.audit_anchor_local_dir))
