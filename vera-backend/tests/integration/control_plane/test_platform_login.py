"""End-to-end platform-operator login (password + mandatory TOTP) against live
Postgres on the RLS-enforcing connection. A platform operator is provisioned as
superuser (NULL-tenant `app_user` + password `user_identity` + global SUPER_ADMIN
grant, MFA pre-enrolled); the app then talks to the DB as the non-superuser role,
so the platform-readable RLS policy is exercised for the whole login path. The
`platform_login_provider` row is seeded by migration 0011 (not by this test).
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pyotp
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.auth import mfa
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
class PlatformWorld:
    user_id: UUID
    email: str
    totp_secret: str


@pytest.fixture
async def platform_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, PlatformWorld]]:
    admin_engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(admin_engine, expire_on_commit=False)
    kms = LocalDevKMS(master_key=_MASTER_KEY)
    user_id = uuid7()
    suffix = user_id.hex[:8]
    email = f"operator-{suffix}@vera.example"

    async with sessionmaker() as session, session.begin():
        permission_ids = await _seed_permissions(session)
        await _seed_system_roles(session, permission_ids)
        super_admin_role = (
            await session.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN'")
            )
        ).scalar_one()
        # NULL-tenant platform operator (account_type CHECK pairs 'platform' with NULL tenant).
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
        identity = UserIdentity(
            tenant_id=None,
            app_user_id=user_id,
            provider_type=ProviderKind.PASSWORD.value,
            provider_subject=email,
            email=email,
            hashed_password=hash_password(PASSWORD),
            mfa_enabled=False,
        )
        # Pre-enroll MFA so login always reaches the verify challenge (MFA is mandatory).
        uri = await mfa.enroll(kms, identity=identity, account_email=email)
        totp_secret = pyotp.parse_uri(uri).secret
        identity.mfa_enabled = True
        session.add(identity)
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
            yield client, PlatformWorld(user_id=user_id, email=email, totp_secret=totp_secret)

    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("DELETE FROM auth_audit_log WHERE app_user_id = :u").bindparams(u=user_id)
        )
        await session.execute(
            text("DELETE FROM user_role WHERE app_user_id = :u").bindparams(u=user_id)
        )
        await session.execute(
            text("DELETE FROM user_identity WHERE app_user_id = :u").bindparams(u=user_id)
        )
        await session.execute(text("DELETE FROM app_user WHERE id = :u").bindparams(u=user_id))
        # System roles/permissions are global; leave them (idempotent re-seed each run).
    await admin_engine.dispose()


async def _authenticate(client: httpx.AsyncClient, world: PlatformWorld) -> str:
    """Full platform login: password → mandatory TOTP → session token."""
    mfa_token = (
        await client.post(
            "/api/v1/platform/auth/login", json={"email": world.email, "password": PASSWORD}
        )
    ).json()["data"]["mfa_token"]
    code = pyotp.TOTP(world.totp_secret).now()
    verified = await client.post(
        "/api/v1/platform/auth/mfa/verify", json={"mfa_token": mfa_token, "code": code}
    )
    token = verified.json()["data"]["session_token"]
    assert isinstance(token, str)
    return token


async def test_login_requires_mfa_then_succeeds(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, world = platform_world
    resp = await client.post(
        "/api/v1/platform/auth/login", json={"email": world.email, "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    # MFA is mandatory: login never mints a session directly.
    assert body["mfa"] == "verify"
    assert body["session_token"] is None
    mfa_token = body["mfa_token"]
    assert mfa_token

    code = pyotp.TOTP(world.totp_secret).now()
    verified = await client.post(
        "/api/v1/platform/auth/mfa/verify", json={"mfa_token": mfa_token, "code": code}
    )
    assert verified.status_code == 200, verified.text
    token = verified.json()["data"]["session_token"]
    assert token

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    md = me.json()["data"]
    assert md["account_type"] == "platform"
    assert md["tenant_id"] is None
    assert md["tenant_slug"] is None
    assert "SUPER_ADMIN" in md["roles"]


async def test_bad_password_is_uniform_401(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, world = platform_world
    resp = await client.post(
        "/api/v1/platform/auth/login", json={"email": world.email, "password": "wrong"}
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["status"] == "FAIL"
    assert body["error_code"] == "UNAUTHORIZED"
    assert body["data"] is None


async def test_unknown_email_is_uniform_401(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, _world = platform_world
    resp = await client.post(
        "/api/v1/platform/auth/login",
        json={"email": "nobody@vera.example", "password": "whatever"},
    )
    assert resp.status_code == 401


async def test_mfa_verify_rejects_wrong_code(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, world = platform_world
    mfa_token = (
        await client.post(
            "/api/v1/platform/auth/login", json={"email": world.email, "password": PASSWORD}
        )
    ).json()["data"]["mfa_token"]
    bad = await client.post(
        "/api/v1/platform/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "000000"}
    )
    assert bad.status_code == 401


async def test_logout_invalidates_platform_session(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, world = platform_world
    token = await _authenticate(client, world)
    auth = {"Authorization": f"Bearer {token}"}

    # The token-scoped /auth/logout is reused as-is for a platform operator — no tenant context.
    assert (await client.get("/api/v1/auth/me", headers=auth)).status_code == 200
    assert (await client.post("/api/v1/auth/logout", headers=auth)).status_code == 200
    # Session is gone — the operator's token no longer authenticates.
    assert (await client.get("/api/v1/auth/me", headers=auth)).status_code == 401


async def test_keepalive_extends_platform_session(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, world = platform_world
    token = await _authenticate(client, world)

    resp = await client.post(
        "/api/v1/auth/session/keepalive", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["Cache-Control"] == "no-store"
    remaining = resp.json()["data"]["expires_in_seconds"]
    assert isinstance(remaining, int)
    assert 0 < remaining <= 3600  # within the default idle window
