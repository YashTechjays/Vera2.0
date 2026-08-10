"""patient_form — the intake aggregate root and the home of `field_answer`.

PHI posture (ADR §5): plaintext under CMEK + RLS + TLS + WORM audit for Phase 1.
Every PHI column carries `info=PHI_INFO` so the later envelope-encryption retrofit
is a metadata walk. Searchable identifiers are **promoted** out of `intake_payload`
into typed, indexed columns (the v1 "ILIKE on JSON = seqscan" fix); the rest stays
in `intake_payload`. Only `patient_name` gets fuzzy (trigram) search — identifier
fields stay exact-match so nothing breaks when they're later encrypted (ADR §5
rule 2). `appointment_date` and `chart_number` are treated as PHI and kept as typed
promoted columns (compliance call confirmed 2026-06-15).
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import PHI_INFO, Base, TenantScopedMixin
from vera_core.models.enums import FormStatus, check_in


class PatientForm(Base, TenantScopedMixin):
    __tablename__ = "patient_form"
    __table_args__ = (
        check_in("status", FormStatus),
        Index("ix_patient_form_tenant_status", "tenant_id", "status"),
        # The queue drain: only the small set of queued rows is indexed,
        # ordered by enqueued_at for FIFO dispatch.
        Index(
            "ix_patient_form_queued",
            "enqueued_at",
            postgresql_where=text("status = 'in_queue'"),
        ),
        # Fuzzy name search (pg_trgm). Only patient_name — see module docstring.
        Index(
            "ix_patient_form_name_trgm",
            text("lower(patient_name) gin_trgm_ops"),
            postgresql_using="gin",
        ),
    )

    # The published schema version this form is bound to (pins the field set the
    # fill-% is computed against). RESTRICT: a bound version can't vanish.
    schema_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("schema_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=FormStatus.READY_FOR_PROCESSING
    )

    intake_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, info=PHI_INFO
    )

    # Promoted searchable identifiers (typed + indexed). Normalized on write
    # (lowercase/trim name, canonical member-id) so a future blind index has a
    # stable input — ADR §5 rule 3.
    patient_name: Mapped[str | None] = mapped_column(String(255), nullable=True, info=PHI_INFO)
    patient_dob: Mapped[date | None] = mapped_column(Date, nullable=True, info=PHI_INFO)
    appointment_date: Mapped[date | None] = mapped_column(Date, nullable=True, info=PHI_INFO)
    chart_number: Mapped[str | None] = mapped_column(String(128), nullable=True, info=PHI_INFO)

    # Worklist display fields promoted out of `intake_payload` so the list query
    # selects typed columns instead of parsing JSON per row (no fuzzy/exact search
    # over them yet, so no index — they're projection-only). Treated as PHI and
    # carried under CMEK like the identifiers above.
    appointment_type: Mapped[str | None] = mapped_column(String(64), nullable=True, info=PHI_INFO)
    member_id: Mapped[str | None] = mapped_column(String(128), nullable=True, info=PHI_INFO)
    insurance_provider: Mapped[str | None] = mapped_column(
        String(255), nullable=True, info=PHI_INFO
    )
    insurance_provider_phone_number: Mapped[str | None] = mapped_column(
        String(64), nullable=True, info=PHI_INFO
    )

    completion_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    # Verified completion: fraction (0-100) of applicable-required fields the judge
    # confirmed (supported + confidence >= floor), NOT mere presence like
    # completion_pct. Set post-call by evaluate_call; drives the retry-fill gate.
    # Nullable (unlike completion_pct): NULL means "not yet evaluated" — a real 0
    # would misread as "0% verified" on the review surface for pre-eval/live forms.
    verified_pct: Mapped[float | None] = mapped_column(Numeric(5, 2), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Why the pipeline sent this form to EXCEPTION_REVIEW (ReviewReason values).
    # NULL outside review and for manual transitions — a pipeline artifact, not
    # a user field. Not PHI.
    review_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The user who queued this form — persisted so the dispatcher can attribute
    # ownership (`call.initiated_by_id`) to the queuer even when the call is
    # actually created later by a different actor (retry-at-callback, freed-slot
    # dispatch). Never overwritten on retry, so ownership survives re-enqueue.
    enqueued_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    # Operator's per-form choice at queue time (voice-lab-style toggle): should the
    # dispatched worker boot the IVR navigator for this call? Defaults FALSE — the
    # operator opts in per enqueue (product decision 2026-08-10; was TRUE). Persisted
    # on the row because dispatch runs later (freed slot / sweeper / auto-retry) and
    # retries keep the choice. Non-PHI config.
    ivr_navigation_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
