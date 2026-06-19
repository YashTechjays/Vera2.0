"""Resolve a URL tenant `slug` to its tenant id, before any tenant RLS context exists.

Tenant-scoped routes carry a human-readable `slug` (`/tenants/{slug}/...`) instead of
the opaque tenant UUID, since a user can't recall a UUID at login. Resolution can't be a
plain `SELECT ... FROM tenant WHERE slug = :slug`: the `tenant` table's RLS keys on `id`
and is fail-closed, so an unpinned session (no `app.tenant_id`) sees zero rows. We go
through the migration-0008 `resolve_tenant_by_slug` SECURITY DEFINER function — the same
sanctioned "read before tenant context" pattern as `elevation` over the 0002 functions.

An unknown or malformed slug resolves to None; callers turn that into the same uniform
401 (pre-auth routes) or 403 (guarded routes) as today's unknown-tenant path, so the
lookup leaks no more than the previous UUID flow.
"""

import re
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Lowercase DNS-label style: starts/ends alphanumeric, hyphens within, 1-63 chars total.
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_slug(raw: str) -> str:
    """Canonical form used for both storage and lookup — slugs are case-insensitive."""
    return raw.strip().lower()


def is_valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.fullmatch(slug))


async def resolve_tenant_id(
    sessionmaker: async_sessionmaker[AsyncSession], raw_slug: str
) -> UUID | None:
    """The tenant id for `raw_slug`, or None if the slug is malformed or unknown.

    Opens a bare (unpinned) session — no tenant GUC — exactly like
    `deps.resolve_elevation`; the SECURITY DEFINER function is what reads past the
    `tenant` RLS policy. Never raises on a missing tenant: None is the not-found signal."""
    slug = normalize_slug(raw_slug)
    if not is_valid_slug(slug):
        return None
    async with sessionmaker() as session:
        tenant_id: UUID | None = (
            await session.execute(text("SELECT resolve_tenant_by_slug(:s)").bindparams(s=slug))
        ).scalar_one()
    return tenant_id
