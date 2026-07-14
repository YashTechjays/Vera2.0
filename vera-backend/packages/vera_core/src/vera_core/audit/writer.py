"""The immutable compliance audit writer.

This is the HIPAA evidence trail, distinct from Langfuse observability. Every
authz allow/deny and every PHI access goes through an AuditSink. The Postgres
sink writes to audit_log, which migration 0001 makes append-only (SELECT/INSERT
RLS policies only, FORCE RLS).

Records must never contain raw PHI — tokens, counts, and entity types only.
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vera_core.db import tenant_session, uuid7
from vera_core.models import AuthAuditLog
from vera_core.models.audit_log import ActorType
from vera_core.models.enums import AuthEvent

logger = logging.getLogger("vera.audit")


@dataclass(frozen=True)
class AuditRecord:
    tenant_id: UUID
    actor_type: ActorType
    event_type: str
    actor_user_id: UUID | None = None
    actor_label: str = ""
    resource_type: str = ""
    resource_id: str = ""
    permission_key: str | None = None
    decision: str | None = None  # "allow" | "deny" for authz events
    request_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    # Set only on a SUPER_ADMIN's elevated request — links the access back to the
    # active tenant_elevation grant (ADR-0006 §B / audit_log.elevation_session_id).
    elevation_session_id: UUID | None = None


class AuditSink(Protocol):
    async def emit(self, record: AuditRecord) -> None: ...


_LOG_AUDIT_EVENT = text(
    "SELECT log_audit_event(:id, :tenant_id, :actor_type, :actor_user_id,"
    " :actor_label, :event_type, :resource_type, :resource_id, :permission_key,"
    " :decision, :request_id, CAST(:detail AS jsonb), :reason,"
    " :elevation_session_id)"
)


class DatabaseAuditWriter:
    """Inserts into audit_log in its OWN short transaction, so the audit record
    survives even when the request that produced it rolls back (a denied or
    failed request still leaves its trail).

    The insert goes through the `log_audit_event` SECURITY DEFINER function —
    one statement + commit instead of GUC set_config + INSERT + COMMIT. Every
    audit emit in the system pays this cost inline on the request path, so it
    is the one place round trips are worth shaving. tenant_id still comes only
    from the server-side AuditRecord, same trust as the GUC path."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def emit(self, record: AuditRecord) -> None:
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                _LOG_AUDIT_EVENT.bindparams(
                    id=uuid7(),
                    tenant_id=record.tenant_id,
                    actor_type=record.actor_type.value,
                    actor_user_id=record.actor_user_id,
                    actor_label=record.actor_label,
                    event_type=record.event_type,
                    resource_type=record.resource_type,
                    resource_id=record.resource_id,
                    permission_key=record.permission_key,
                    decision=record.decision,
                    request_id=record.request_id,
                    detail=json.dumps(record.detail),
                    reason=record.reason,
                    elevation_session_id=record.elevation_session_id,
                )
            )


class LoggingAuditSink:
    """Structured-log sink for local dev and unit tests. NOT a compliance store."""

    async def emit(self, record: AuditRecord) -> None:
        logger.info("audit %s", json.dumps(asdict(record), default=str, sort_keys=True))


@dataclass(frozen=True)
class AuthAuditRecord:
    """An authN/Z event for auth_audit_log (login/MFA/role/elevation/provider).
    Carries no PHI — identifiers, event type, and non-sensitive metadata only."""

    tenant_id: UUID | None
    event_type: str  # an AuthEvent value
    app_user_id: UUID | None = None
    ip_address: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class AuthAuditSink(Protocol):
    async def emit(self, record: AuthAuditRecord) -> None: ...


async def emit_auth_event(
    sink: AuthAuditSink,
    *,
    tenant_id: UUID | None,
    event: AuthEvent,
    ip: str | None,
    user_id: UUID | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Write one authN/Z event to the auth audit log. The single construction
    point for `AuthAuditRecord` across every caller — auth, admin, and platform
    routes — so a new call site can't hand-roll the record and drift on shape."""
    await sink.emit(
        AuthAuditRecord(
            tenant_id=tenant_id,
            app_user_id=user_id,
            event_type=event.value,
            ip_address=ip,
            meta=meta or {},
        )
    )


class DatabaseAuthAuditWriter:
    """Writes auth_audit_log in its own short transaction (like
    DatabaseAuditWriter) so the trail survives a failed/denied login that rolls
    back. A tenant-scoped event inserts under that tenant's RLS GUC; a null-tenant
    (platform) event has no GUC that the WORM insert policy could match, so it goes
    through the `log_auth_event` SECURITY DEFINER function — the sanctioned platform
    write path (ADR-0006 §C)."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def emit(self, record: AuthAuditRecord) -> None:
        if record.tenant_id is None:
            async with self._sessionmaker() as session, session.begin():
                await session.execute(
                    text(
                        "SELECT log_auth_event(NULL, :u, :e, CAST(:ip AS inet),"
                        " CAST(:meta AS jsonb))"
                    ).bindparams(
                        u=record.app_user_id,
                        e=record.event_type,
                        ip=record.ip_address,
                        meta=json.dumps(record.meta),
                    )
                )
            return
        async with tenant_session(self._sessionmaker, record.tenant_id) as session:
            session.add(
                AuthAuditLog(
                    tenant_id=record.tenant_id,
                    app_user_id=record.app_user_id,
                    event_type=record.event_type,
                    ip_address=record.ip_address,
                    meta=record.meta,
                )
            )


class LoggingAuthAuditSink:
    """Structured-log auth sink for local dev and unit tests. NOT a compliance store."""

    async def emit(self, record: AuthAuditRecord) -> None:
        logger.info("auth-audit %s", json.dumps(asdict(record), default=str, sort_keys=True))
