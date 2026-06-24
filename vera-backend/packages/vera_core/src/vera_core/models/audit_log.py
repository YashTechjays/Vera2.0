"""The compliance audit log — immutable, in Postgres, separate from Langfuse
(Langfuse is observability; this table is the HIPAA evidence trail).

Immutability is enforced in the migration: RLS grants only SELECT and INSERT
policies, so UPDATE/DELETE are denied for every connection without BYPASSRLS,
including the table owner (FORCE ROW LEVEL SECURITY)."""

import enum
from typing import Any
from uuid import UUID

from sqlalchemy import Enum, ForeignKey, LargeBinary, String, Text
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
    # A reviewer adjudicated disputed fields on a patient_form (accept/override/
    # correct + re-ask). Field names/counts only — never the values.
    DISPUTE_RESOLVE = "dispute.resolve"
    # A human changed a patient_form's lifecycle status by hand (the dedicated
    # status endpoint). Records from/to status only — statuses are not PHI.
    FORM_STATUS_CHANGE = "form.status_change"


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

    # WORM per-row hash chain (ADR §7 / ERD). Populated by the audit writer when
    # chaining is enabled; nullable so existing inserts remain valid.
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    row_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
