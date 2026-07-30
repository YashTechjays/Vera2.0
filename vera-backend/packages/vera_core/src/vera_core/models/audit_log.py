"""The compliance audit log — immutable, in Postgres, separate from Langfuse
(Langfuse is observability; this table is the HIPAA evidence trail).

Immutability is enforced in the migration: RLS grants only SELECT and INSERT
policies, so UPDATE/DELETE are denied for every connection without BYPASSRLS,
including the table owner (FORCE ROW LEVEL SECURITY)."""

import enum
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Enum, FetchedValue, ForeignKey, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from vera_core.db.base import Base, TenantScopedMixin


class ActorType(enum.StrEnum):
    USER = "user"
    SERVICE = "service"
    SYSTEM = "system"


class AuditEvent(enum.StrEnum):
    AUTHZ_ALLOW = "authz.allow"
    AUTHZ_DENY = "authz.deny"
    PHI_ACCESS = "phi.access"
    PHI_DETOKENIZE = "phi.detokenize"
    PHI_HYDRATE_FAILSAFE = "phi.hydrate_failsafe"
    INTEGRATION_CONFIGURE = "integration.configure"
    # Inbound API-key intake: a machine caller created a patient_form + its
    # INTAKE-source field_answer rows (a PHI write). Field names/counts only.
    FORM_INTAKE = "form.intake"
    # The Observer extracted an answer from a live call and the worker-event
    # consumer wrote it as an ai_call field_answer (a PHI write). Field path +
    # call id only — never the value.
    FORM_AI_ANSWER = "form.ai_answer"
    # A reviewer adjudicated disputed fields on a patient_form (accept/override/
    # correct + re-ask). Field names/counts only — never the values.
    DISPUTE_RESOLVE = "dispute.resolve"
    # A human changed a patient_form's lifecycle status by hand (the dedicated
    # status endpoint). Records from/to status only — statuses are not PHI.
    FORM_STATUS_CHANGE = "form.status_change"
    # A VA published a call so other tenant VAs can view it (visibility
    # widening — a disclosure-enabling decision). Ids only, never PHI.
    CALL_PUBLISH = "call.publish"
    # A VA (owner or not) minted a listen-only join token for a call — the
    # actual PHI disclosure (they can now hear the live transcript). Ids only.
    CALL_LISTEN_ONLY_JOIN = "call.listen-only.join"
    # A publish-capable (speaking) join (?intervene=true). Distinct from
    # listen-only from day one; the full intervention feature (agent takeover
    # behavior) is still TODO.
    CALL_INTERVENE_JOIN = "call.intervene.join"
    # A VA ended a live call (LiveKit room torn down; the worker's call.ended
    # event drives the actual closeout). Ids only, never PHI.
    CALL_END = "call.end"
    # Queue dispatch: the dispatcher picked a form off the queue and initiated a
    # call. Records form id + tenant — no PHI field values.
    QUEUE_DISPATCH = "queue.dispatch"
    # Queue expiry: the dispatcher marked a form expired because it exceeded the
    # tenant's queue_expiry_hours window. Records form id + tenant only.
    QUEUE_EXPIRED = "queue.expired"
    # A user exported a completed form as a file — PHI left the perimeter.
    # Detail carries artifact id, format, and field NAMES only, never values.
    FORM_EXPORTED = "form.exported"
    # Recording lifecycle (call audio in GCS). Ids/hashes/sizes only — never audio,
    # never PHI. RECORDING_DELETED is emitted twice per sweep: detail.phase="before"
    # (object snapshot pre-delete) and "after" (verified-gone confirmation).
    RECORDING_START_FAILED = "recording.start_failed"
    RECORDING_FAILED = "recording.failed"
    RECORDING_DISCARDED = "recording.discarded"
    RECORDING_ACCESSED = "recording.accessed"
    RECORDING_DELETED = "recording.deleted"


class AuditLog(Base, TenantScopedMixin):
    __tablename__ = "audit_log"

    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, name="actor_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # User.id for humans; service-account email / worker identity for services.
    actor_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    actor_label: Mapped[str] = mapped_column(String(320), nullable=False, default="")

    # Set only when a SUPER_ADMIN read this row while elevated into the tenant —
    # links the PHI access back to the tenant_elevation grant (ADR §3.5.4 req 3),
    # answering "which human, and why they were in this tenant".
    elevation_session_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenant_elevation.id", ondelete="SET NULL"),
        nullable=True,
    )

    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # Often the endpoint path for authz audits; nested tenant routes (e.g.
    # /tenants/{id}/users/{id}/roles/{id}) carry several UUIDs, so 512 not 128.
    resource_id: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    permission_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)  # allow | deny

    request_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # Non-PHI metadata only — raw PHI never lands here, tokens are fine.
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # WORM per-row hash chain (ADR §7 / ERD). All three columns are populated by
    # the audit_chain() BEFORE INSERT trigger (migration 0015) — never by the
    # writer. FetchedValue on seq tells SQLAlchemy the column is DB-assigned so
    # it is omitted from INSERT and refreshed after commit.
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    row_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    seq: Mapped[int] = mapped_column(BigInteger, FetchedValue(), nullable=False)
