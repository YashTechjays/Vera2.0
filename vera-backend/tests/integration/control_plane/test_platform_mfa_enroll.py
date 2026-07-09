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
    # Platform MFA is TOTP-only — no recovery codes in the contract.
    assert "recovery_codes" not in body

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


async def _activate(client: httpx.AsyncClient, data: dict[str, Any]) -> None:
    secret = pyotp.parse_uri(data["provisioning_uri"]).secret
    resp = await client.post(
        "/api/v1/platform/auth/mfa/enroll-activate",
        json={"mfa_token": data["mfa_token"], "code": pyotp.TOTP(secret).now()},
    )
    assert resp.status_code == 200, resp.text


async def test_enroll_token_replay_after_activation_is_401(
    enroll_world: tuple[httpx.AsyncClient, EnrollWorld],
) -> None:
    client, world = enroll_world
    data = await _login(client, world.email)
    await _activate(client, data)

    secret = pyotp.parse_uri(data["provisioning_uri"]).secret
    replay = await client.post(
        "/api/v1/platform/auth/mfa/enroll-activate",
        json={"mfa_token": data["mfa_token"], "code": pyotp.TOTP(secret).now()},
    )
    assert replay.status_code == 401


async def test_non_totp_code_at_verify_is_clean_401(
    enroll_world: tuple[httpx.AsyncClient, EnrollWorld],
) -> None:
    """TOTP-only: a would-be recovery code at /mfa/verify is a uniform 401, never a 500
    (there are no recovery codes to consume, and verify never writes the row)."""
    client, world = enroll_world
    await _activate(client, await _login(client, world.email))

    again = await _login(client, world.email)
    assert again["mfa"] == "verify"
    resp = await client.post(
        "/api/v1/platform/auth/mfa/verify",
        json={"mfa_token": again["mfa_token"], "code": "0123456789"},
    )
    assert resp.status_code == 401


async def _identity_id(database_url: str, email: str) -> UUID:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            ident_id: UUID = (
                await conn.execute(
                    text(
                        "SELECT ui.id FROM user_identity ui "
                        "JOIN app_user u ON u.id = ui.app_user_id WHERE u.email = :e"
                    ).bindparams(e=email)
                )
            ).scalar_one()
            return ident_id
    finally:
        await engine.dispose()


async def test_definer_write_refused_outside_platform_context(
    enroll_world: tuple[httpx.AsyncClient, EnrollWorld],
    database_url: str,
    rls_database_url: str,
) -> None:
    """The definer functions require the platform GUC (app.platform='on'): a connection
    without it — e.g. a tenant-scoped one — is a no-op, so a compromised tenant request
    can't plant a seed on a not-yet-enrolled operator."""
    client, world = enroll_world
    await _login(client, world.email)  # mints the legitimate seed
    ident_id = await _identity_id(database_url, world.email)

    rls_engine = create_async_engine(rls_database_url)
    try:
        async with rls_engine.connect() as conn:
            stored = (
                await conn.execute(
                    text(
                        "SELECT platform_store_mfa_seed(CAST(:id AS uuid), :seed, :dek, 'x')"
                    ).bindparams(id=ident_id, seed=b"attacker", dek=b"attacker")
                )
            ).scalar_one()
        assert stored is False
    finally:
        await rls_engine.dispose()


async def test_store_seed_refuses_enrolled_identity(
    enroll_world: tuple[httpx.AsyncClient, EnrollWorld],
    database_url: str,
    rls_database_url: str,
) -> None:
    """Even in a platform context, the enrollment window closes at activation: the
    mfa_enabled=false guard makes the seed store a no-op on an enrolled operator."""
    client, world = enroll_world
    await _activate(client, await _login(client, world.email))
    ident_id = await _identity_id(database_url, world.email)

    rls_engine = create_async_engine(rls_database_url)
    try:
        async with rls_engine.begin() as conn:
            await conn.execute(text("SELECT set_config('app.platform', 'on', true)"))
            stored = (
                await conn.execute(
                    text(
                        "SELECT platform_store_mfa_seed(CAST(:id AS uuid), :seed, :dek, 'x')"
                    ).bindparams(id=ident_id, seed=b"attacker", dek=b"attacker")
                )
            ).scalar_one()
        assert stored is False
    finally:
        await rls_engine.dispose()
