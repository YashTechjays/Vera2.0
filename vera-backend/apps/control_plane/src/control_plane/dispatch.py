"""Self-contained dispatch pass, run OUTSIDE any request transaction.

The dispatcher makes external calls (LiveKit room + SIP dial) and sleeps between
dials for carrier pacing — none of that may hold an HTTP request's transaction or
row locks. Hosts: the status endpoint's post-commit background task and the
worker-event consumer (a call ended → a slot freed).
"""

import logging
from typing import TYPE_CHECKING, Any

from vera_core.db.rls import tenant_session
from vera_core.services.queue_dispatcher import try_dispatch

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from vera_core.audit import AuditSink

logger = logging.getLogger(__name__)


async def run_dispatch_pass(
    sessionmaker: "async_sessionmaker[AsyncSession]",
    tenant_id: "UUID",
    livekit: Any,
    kms: Any,
    audit: "AuditSink | None",
) -> None:
    """One dispatch pass in a fresh tenant-scoped session; commits on success.
    Exception-safe: a failed pass logs and returns — queued forms are retried on
    the next triggering event."""
    try:
        async with tenant_session(sessionmaker, tenant_id) as session:
            await try_dispatch(session, tenant_id, livekit, kms, audit=audit)
    except Exception:
        logger.exception("dispatch pass failed for tenant %s", tenant_id)
