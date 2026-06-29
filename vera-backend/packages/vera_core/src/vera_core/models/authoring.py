"""Schema/prompt authoring catalog — a GLOBAL, non-PHI, cross-tenant catalog.

These tables have **no** `tenant_id` and **no** RLS: per ADR §3.5.4 the schema /
prompt authoring catalog is a platform surface a SUPER_ADMIN curates, and a
published version is then *bound* by a tenant's `patient_form`. The version chains
(`schema_version`, `prompt_version`) are the spec's headline win — call → prompt →
schema traceability — so versions are immutable rows, never edited in place.

Field definitions deliberately live in `schema_version.schema_json` (the DSL) and
are *compiled* into `prompt_version.composite_json`; they are not exploded into
relational rows (ADR §1, §8).
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDv7PKMixin
from vera_core.models.enums import InsuranceType, VersionStatus, check_in


class FormSchema(Base, UUIDv7PKMixin, TimestampMixin):
    """The schema family (e.g. "BCBS prior-auth"); versions hang off it.

    `insurance_type` is constrained to the `InsuranceType` catalog (CHECK, never
    free text) and is UNIQUE — exactly one schema family per insurance type.
    """

    __tablename__ = "form_schema"
    __table_args__ = (
        check_in("insurance_type", InsuranceType),
        UniqueConstraint("insurance_type"),
    )

    insurance_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class SchemaVersion(Base, UUIDv7PKMixin, CreatedAtMixin):
    """One immutable version of a form schema. `schema_json` is the DSL document
    the composite prompt is generated from."""

    __tablename__ = "schema_version"
    __table_args__ = (
        UniqueConstraint("schema_id", "version"),
        check_in("status", VersionStatus),
        # At most one published version per schema family — so "the published
        # schema for this insurance type" is a single indexed lookup, and
        # promoting a new version requires demoting the old one first.
        Index(
            "uq_schema_version_published_per_schema",
            "schema_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )

    schema_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("form_schema.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=VersionStatus.DRAFT)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Prompt(Base, UUIDv7PKMixin, TimestampMixin):
    """A prompt family generated from a schema (catalog-level)."""

    __tablename__ = "prompt"

    schema_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("form_schema.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class PromptVersion(Base, UUIDv7PKMixin, CreatedAtMixin):
    """One immutable prompt version, tagged to the exact `schema_version` it was
    generated from — this is the call→prompt→schema lineage the v1 DB lacked.
    `call.prompt_version_id` pins which version ran a call."""

    __tablename__ = "prompt_version"
    __table_args__ = (
        UniqueConstraint("prompt_id", "version"),
        check_in("status", VersionStatus),
        # At most one published version per prompt family — the mirror of
        # uq_schema_version_published_per_schema. "The published prompt for this
        # family" is a single indexed lookup, and promoting a new version requires
        # demoting the old one first.
        Index(
            "uq_prompt_version_published_per_prompt",
            "prompt_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )

    prompt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("prompt.id", ondelete="CASCADE"), nullable=False
    )
    schema_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("schema_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    composite_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=VersionStatus.DRAFT)
