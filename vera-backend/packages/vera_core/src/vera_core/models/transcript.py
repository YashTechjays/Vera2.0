"""transcript + recording.

Transcript fixes the v1 P5 bug the spec ERD reproduced: `seq` is NOT NULL with a
`UNIQUE(call_id, seq)`, and `message` is unbounded TEXT — fail loud at insert, no
silent truncation, no NULL-seq ordering scans. `message` is PHI. Recording stores
only the CMEK-encrypted GCS object URI (a pointer, not PHI) + its retention clock and lifecycle.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import (
    PHI_INFO,
    Base,
    CreatedAtMixin,
    TenantColumnMixin,
    UUIDv7PKMixin,
)
from vera_core.models.enums import RecordingStatus, TranscriptSource, check_in


class Transcript(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    __tablename__ = "transcript"
    __table_args__ = (
        UniqueConstraint("call_id", "seq"),
        check_in("source", TranscriptSource),
    )

    call_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("call.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    message: Mapped[str] = mapped_column(Text, nullable=False, info=PHI_INFO)
    spoke_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Recording(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    __tablename__ = "recording"
    __table_args__ = (check_in("status", RecordingStatus),)

    call_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("call.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gcs_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RecordingStatus.PENDING.value
    )
    egress_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Evidence columns survive deletion (the tombstone keeps proving WHAT was destroyed).
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
