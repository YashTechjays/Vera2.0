"""Browser-based platform-operator MFA enrollment (first-login wall).

A platform operator is bootstrapped WITHOUT MFA (mfa_enabled=False, no seed). The
first /platform/auth/login returns an `enroll` challenge + provisioning URI; the
operator confirms a live TOTP code at /platform/auth/mfa/enroll-activate, which flips
mfa_enabled via the SECURITY DEFINER path (the app role can't UPDATE the NULL-tenant
row directly). The app talks to the DB as the non-superuser RLS role, so these tests
exercise the definer functions for real.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import pyotp
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.auth.password import hash_password
from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.auth.session import InMemorySessionStore
from control_plane.main import create_app
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.models import AppUser, UserIdentity, UserRole
from vera_core.models.enums import ProviderKind

PASSWORD = "correct horse battery staple"
_MASTER_KEY = b"a" * 32


@dataclass
class EnrollWorld:
    user_id: UUID
    email: str


@pytest.fixture
async def enroll_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, EnrollWorld]]:
    admin_engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(admin_engine, expire_on_commit=False)
    user_id = uuid7()
    email = f"operator-{user_id.hex[:8]}@vera.example"

    async with sessionmaker() as session, session.begin():
        permission_ids = await _seed_permissions(session)
        await _seed_system_roles(session, permission_ids)
        super_admin_role = (
            await session.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN'")
            )
        ).scalar_one()
        session.add(
            AppUser(
                id=user_id,
                tenant_id=None,
                account_type="platform",
                gcip_uid=None,
                email=email,
                name="Operator",
                status="active",
            )
        )
        await session.flush()
        # Bootstrapped WITHOUT MFA — no seed, mfa_enabled=False.
        session.add(
            UserIdentity(
                tenant_id=None,
                app_user_id=user_id,
                provider_type=ProviderKind.PASSWORD.value,
                provider_subject=email,
                email=email,
                hashed_password=hash_password(PASSWORD),
                mfa_enabled=False,
            )
        )
        session.add(UserRole(tenant_id=None, app_user_id=user_id, role_id=super_admin_role))

    settings = Settings(_env_file=None, database_url=rls_database_url)
    app = create_app(
        settings,
        session_store=InMemorySessionStore(),
        kms=LocalDevKMS(master_key=_MASTER_KEY),
        permission_cache=InMemoryPermissionCache(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, EnrollWorld(user_id=user_id, email=email)

    async with sessionmaker() as session, session.begin():
        for tbl in ("auth_audit_log", "user_role", "user_identity"):
            await session.execute(
                text(f"DELETE FROM {tbl} WHERE app_user_id = :u").bindparams(u=user_id)
            )
        await session.execute(text("DELETE FROM app_user WHERE id = :u").bindparams(u=user_id))
    await admin_engine.dispose()


async def _login(client: httpx.AsyncClient, email: str) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/platform/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = resp.json()["data"]
    return data


async def test_first_login_returns_enroll_challenge_with_qr(
    enroll_world: tuple[httpx.AsyncClient, EnrollWorld],
) -> None:
    client, world = enroll_world
    data = await _login(client, world.email)
    assert data["mfa"] == "enroll"
    assert data["mfa_token"]
    assert data["provisioning_uri"].startswith("otpauth://")
    # A scannable secret is embedded.
    assert pyotp.parse_uri(data["provisioning_uri"]).secret


async def test_enroll_activate_completes_setup_and_logs_in(
    enroll_world: tuple[httpx.AsyncClient, EnrollWorld],
) -> None:
    client, world = enroll_world
    data = await _login(client, world.email)
    secret = pyotp.parse_uri(data["provisioning_uri"]).secret

    activated = await client.post(
        "/api/v1/platform/auth/mfa/enroll-activate",
        json={"mfa_token": data["mfa_token"], "code": pyotp.TOTP(secret).now()},
    )
    assert activated.status_code == 200, activated.text
    body = activated.json()["data"]
    assert body["session_token"]
    assert len(body["recovery_codes"]) == 10

    # A second login now goes straight to the verify challenge (MFA is set up).
    again = await _login(client, world.email)
    assert again["mfa"] == "verify"


async def test_enroll_activate_wrong_code_is_401(
    enroll_world: tuple[httpx.AsyncClient, EnrollWorld],
) -> None:
    client, world = enroll_world
    data = await _login(client, world.email)
    bad = await client.post(
        "/api/v1/platform/auth/mfa/enroll-activate",
        json={"mfa_token": data["mfa_token"], "code": "000000"},
    )
    assert bad.status_code == 401
    # Still unenrolled — a fresh login returns the enroll challenge again.
    again = await _login(client, world.email)
    assert again["mfa"] == "enroll"
