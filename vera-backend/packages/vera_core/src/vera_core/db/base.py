"""Declarative base and the column mixins every Vera table is built from.

Primary keys are UUIDv7 (time-ordered, index-friendly) generated client-side so
they exist before flush and work without a DB round-trip; the column is the
native Postgres `uuid` type, never text.

Mixin menu (compose to taste):
- `UUIDv7PKMixin`     — the `id` PK.
- `TimestampMixin`    — `created_at` + `updated_at` (mutable rows).
- `CreatedAtMixin`    — `created_at` only (append-only / WORM log rows).
- `TenantColumnMixin` — the RLS-keyed `tenant_id` FK (+ index), nothing else.
- `TenantScopedMixin` — id + tenant_id + created/updated (the common shape).

PHI columns carry `info=PHI_INFO` so the eventual plaintext->encrypted retrofit
(see adr/vera2-database-design.md §5) is a one-pass `Base.metadata` walk, not a
codebase grep. The marker is metadata only — it does not encrypt anything today;
application-level column encryption stays deferred per the repo CLAUDE.md.
"""

from datetime import datetime
from typing import Any, Final
from uuid import UUID

import uuid_utils
from sqlalchemy import DateTime, ForeignKey, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Marker for PHI-bearing columns. Pass as `mapped_column(..., info=PHI_INFO)`.
# Today it is purely descriptive; at the encryption retrofit it becomes the
# single source of "which columns to wrap".
PHI_INFO: Final[dict[str, Any]] = {"phi": True}


def uuid7() -> UUID:
    # uuid_utils returns its own UUID class; convert to stdlib for SQLAlchemy/asyncpg.
    return UUID(bytes=uuid_utils.uuid7().bytes)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDv7PKMixin:
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CreatedAtMixin:
    """Just `created_at` — for append-only / immutable log rows that are never
    updated in place (transcripts, call events, audit, evaluations, …)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TenantColumnMixin:
    """The RLS-keyed `tenant_id` and nothing else.

    Carried by every tenant-scoped table — including child/append-only tables
    where `tenant_id` is *denormalized* from the parent so the RLS policy stays
    a plain column comparison (`tenant_id = current_setting(...)`) rather than an
    EXISTS subquery on every read. ondelete=RESTRICT: a tenant cannot be hard
    deleted out from under live PHI; lifecycle is via soft-delete.
    """

    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


class NullableTenantColumnMixin:
    """A NULLABLE RLS-keyed `tenant_id` — the platform-tier sibling of
    `TenantColumnMixin` (ADR §3.5.9).

    Used by the identity/RBAC tables that span tenants: `app_user` (a platform
    operator has `tenant_id IS NULL`), and `role`/`role_permission` (system roles
    are global, `tenant_id IS NULL`; custom roles are tenant-scoped). Strict RLS
    still applies — a NULL row never matches a tenant GUC, so it is invisible to
    tenant sessions (fail-closed). `tenant_id` is a *scope* attribute here, never
    a privilege one; privilege comes only from an RBAC role assignment.
    """

    tenant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenant.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )


class TenantScopedMixin(UUIDv7PKMixin, TimestampMixin, TenantColumnMixin):
    """The standard shape: UUIDv7 id + tenant_id + created/updated timestamps.

    Every mutable tenant-scoped (and especially every PHI-bearing) table inherits
    this, and its tenant_id is what the RLS policies key on.
    """
