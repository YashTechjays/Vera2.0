"""Export disclosure ledger — one row per file that left the perimeter.

No file is stored: sha256 identifies the exact bytes streamed to the caller;
the paired FORM_EXPORTED audit record carries who/when/what-fields. `gcs_uri`
is reserved for a future stored-artifact variant and stays NULL today.
"""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import Base, CreatedAtMixin, TenantColumnMixin, UUIDv7PKMixin


class ExportArtifact(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    __tablename__ = "export_artifact"
    __table_args__ = (CheckConstraint("format IN ('xlsx')", name="format_valid"),)

    form_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("patient_form.id", ondelete="RESTRICT"),
        index=True,
    )
    format: Mapped[str] = mapped_column(String(8))
    sha256: Mapped[str] = mapped_column(String(64))
    gcs_uri: Mapped[str | None] = mapped_column(String(512))
    exported_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
