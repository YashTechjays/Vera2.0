"""WORM anchoring of audit_log chain heads to an object-locked external store.

Only DIGESTS leave the DB — per-tenant chain heads (hashes + counts), never PHI
rows. The job is pull-based (run by a CronJob); see the GCSAnchorSink for prod
and LocalFilesystemAnchorSink for dev/test. build_anchor_sink mirrors build_kms.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

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


GENESIS_ANCHOR = b"\x00" * 32


def _serialize(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_anchor_object(
    heads: list[ChainHead],
    prev_anchor_sha256: bytes,
    run_id: UUID,
    anchored_at: str,
) -> tuple[dict[str, Any], bytes]:
    core: dict[str, Any] = {
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
            key=lambda c: str(c["tenant_id"]),
        ),
    }
    anchor_sha = hashlib.sha256(_serialize(core)).hexdigest()
    obj: dict[str, Any] = {**core, "anchor_sha256": anchor_sha}
    return obj, _serialize(obj)


def anchor_key(anchored_at: str, run_id: UUID) -> str:
    y, m, d = anchored_at[:10].split("-")
    return f"anchors/{y}/{m}/{d}/{anchored_at}-{run_id}.json"


async def _db_now_utc(session: AsyncSession) -> str:
    return await session.scalar(  # type: ignore[no-any-return]
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
    sm: async_sessionmaker[AsyncSession], anchor_obj: dict[str, Any]
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    async with sm() as session:
        for chain in anchor_obj["chains"]:
            tid = UUID(chain["tenant_id"])
            broken = await session.scalar(text("SELECT verify_audit_chain(:t)").bindparams(t=tid))
            if broken is not None:
                mismatches.append(
                    {"tenant_id": chain["tenant_id"], "reason": "chain_broken", "seq": broken}
                )
                continue
            row_hash = await session.scalar(
                text("SELECT audit_row_hash_at(:t, :s)").bindparams(t=tid, s=chain["head_seq"])
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


def build_anchor_sink(settings: "Settings") -> AnchorSink:
    if settings.audit_anchor_bucket:
        from vera_core.audit.gcs_anchor import (
            GCSAnchorSink,  # lazy: prod only
        )

        return GCSAnchorSink(settings.audit_anchor_bucket, settings.audit_anchor_prefix)
    return LocalFilesystemAnchorSink(Path(settings.audit_anchor_local_dir))
