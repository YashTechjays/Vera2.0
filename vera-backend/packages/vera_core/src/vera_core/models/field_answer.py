"""The form-data model — the central decision (ADR §4).

`field_answer` keys every captured value to the **form** (not the call), carrying
its `call_id` provenance and an `is_current` flag. A partial unique index
guarantees exactly one current value per field no matter how many calls
contributed — so "the current form" is one indexed query, never an N-instance
merge in Python (the v1 P1 fix). `call_form_snapshot` keeps the spec's legitimate
per-call before/after snapshot as an immutable artifact. The dispute signal is derived
purely from `field_answer` history: a field is disputed when its current row came from
the AI call and its value diverges from the most recent `intake`/`human` baseline.
`field_evaluation` is unrelated advisory metadata (confidence/sort) and plays no part in
disputes. `dispute_action` is a pure audit record of a human adjudication and does not
gate the dispute; resolutions emit a NEW `field_answer` (source=human) — advancing the
baseline — rather than mutating in place.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import (
    PHI_INFO,
    Base,
    CreatedAtMixin,
    TenantColumnMixin,
    UUIDv7PKMixin,
)
from vera_core.models.enums import AnswerSource, DisputeActionType, check_in


class FieldAnswer(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    __tablename__ = "field_answer"
    __table_args__ = (
        # Exactly one current value per (form, field) — the merge invariant.
        Index(
            "fa_current_uq",
            "form_id",
            "field_path",
            unique=True,
            postgresql_where=text("is_current"),
        ),
        Index("ix_field_answer_call", "call_id"),
        # Baseline lookup: most recent intake/human answer per (form, field). Backs the
        # dispute derivation (DISTINCT ON … ORDER BY created_at DESC) on the worklist.
        Index(
            "ix_field_answer_baseline",
            "form_id",
            "field_path",
            text("created_at DESC"),
            text("id DESC"),
        ),
        check_in("source", AnswerSource),
        CheckConstraint("confidence BETWEEN 0 AND 100", name="confidence_range"),
    )

    form_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("patient_form.id", ondelete="CASCADE"), nullable=False
    )
    # NULL for intake- or human-sourced answers (no originating call).
    call_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("call.id", ondelete="SET NULL"), nullable=True
    )
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, info=PHI_INFO)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # Pointer into transcript.seq for the supporting evidence.
    evidence_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True, info=PHI_INFO)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CallFormSnapshot(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    """One frozen before/after JSONB per call — the spec's snapshot/isolation
    requirement, kept as an immutable per-call audit artifact (1-1 with call).
    before_state is written at dispatch; after_state stays `{}` (the reserved
    not-yet-finalized sentinel) until the post-call eval fills it."""

    __tablename__ = "call_form_snapshot"
    __table_args__ = (UniqueConstraint("call_id"),)

    call_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("call.id", ondelete="CASCADE"), nullable=False
    )
    before_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, info=PHI_INFO
    )
    after_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, info=PHI_INFO
    )


class FieldEvaluation(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    """Post-call LLM-as-judge verdict for one answer (the second pass)."""

    __tablename__ = "field_evaluation"
    __table_args__ = (CheckConstraint("confidence BETWEEN 0 AND 100", name="confidence_range"),)

    answer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("field_answer.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    confidence: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True, info=PHI_INFO)
    supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class DisputeAction(Base, UUIDv7PKMixin, CreatedAtMixin, TenantColumnMixin):
    """A human adjudication of an answer. A correction emits a new current
    `field_answer` (source=human); this row is the audited record of the decision."""

    __tablename__ = "dispute_action"
    __table_args__ = (check_in("action", DisputeActionType),)

    answer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("field_answer.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, info=PHI_INFO)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True, info=PHI_INFO)
