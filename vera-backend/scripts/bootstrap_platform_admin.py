"""Idempotent bootstrap for platform operator #1 (ADR-0006 §D).

Platform endpoints require an existing SUPER_ADMIN, but none exists out of the box
(`seed.py` makes only a TENANT_ADMIN). This run-once script seeds exactly the FIRST
operator — a platform `app_user` (account_type='platform', tenant_id=NULL) + password
`user_identity` (MFA left unenrolled) + a grant of the global SUPER_ADMIN role. The operator
sets up 2FA in the browser on first /platform/login, which is only open for a short window
after this bootstrap (so a leaked password can't enroll long after setup; see
`platform_enroll_window_seconds`). No terminal QR. From then on, operators add each other via
the platform invite flow (separate plan).

Runs as the DB user from VERA_DATABASE_URL (locally the superuser → bypasses RLS), exactly
like seed.py — so the NULL-tenant inserts are permitted. NO-OP if any platform operator
already exists.

    just bootstrap-platform   (env: BOOTSTRAP_ADMIN_EMAIL, BOOTSTRAP_ADMIN_PASSWORD)
"""

import asyncio
import os

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.auth.password import hash_password
from vera_core.config import get_settings
from vera_core.db import create_engine, create_sessionmaker
from vera_core.models import AppUser, Role, UserIdentity, UserRole
from vera_core.models.enums import AccountType, ProviderKind


async def _platform_operator_exists(session: AsyncSession) -> bool:
    count = (
        await session.execute(
            select(func.count())
            .select_from(AppUser)
            .where(AppUser.account_type == AccountType.PLATFORM.value)
        )
    ).scalar_one()
    return count > 0


async def bootstrap(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    email: str,
    password: str,
) -> bool:
    """Create platform operator #1, returning True; or False (no-op) if a platform operator
    already exists. MFA is left unenrolled — the operator completes the browser enrollment
    wall on first /platform/login, which is only open for a short window after this bootstrap
    (`platform_enroll_window_seconds`), so the password alone can't enroll later (ADR-0006 §D)."""
    async with sessionmaker() as session, session.begin():
        if await _platform_operator_exists(session):
            return False

        super_admin = (
            await session.execute(
                select(Role).where(Role.tenant_id.is_(None), Role.name == "SUPER_ADMIN")
            )
        ).scalar_one_or_none()
        if super_admin is None:
            raise RuntimeError("SUPER_ADMIN role not found — run `just seed` (or migrate) first")

        user = AppUser(
            tenant_id=None,
            account_type=AccountType.PLATFORM.value,
            gcip_uid=None,
            email=email,
            name="Platform Operator",
            status="active",
        )
        session.add(user)
        await session.flush()

        identity = UserIdentity(
            tenant_id=None,
            app_user_id=user.id,
            provider_type=ProviderKind.PASSWORD.value,
            provider_subject=email,
            email=email,
            hashed_password=hash_password(password),
            mfa_enabled=False,
        )
        session.add(identity)
        session.add(UserRole(tenant_id=None, app_user_id=user.id, role_id=super_admin.id))

    return True


async def main() -> None:
    email = os.environ["BOOTSTRAP_ADMIN_EMAIL"]
    password = os.environ["BOOTSTRAP_ADMIN_PASSWORD"]
    settings = get_settings()
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    try:
        created = await bootstrap(sessionmaker, email=email, password=password)
        if not created:
            print("platform operator already exists — no-op")
        else:
            print(f"created platform operator {email!r} (SUPER_ADMIN)")
            print("Set up 2FA in the browser at /platform/login within")
            print(f"{settings.platform_enroll_window_seconds // 60} minutes of this bootstrap.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
