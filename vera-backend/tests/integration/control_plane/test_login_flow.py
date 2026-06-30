"""End-to-end password + MFA login against live Postgres (RLS-enforcing
connection). A fresh tenant is provisioned as superuser; the app talks to the DB
as the non-superuser role, so RLS is in force for the whole login path. The
session store / secret provider are in-memory so the test owns their state.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
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
from vera_core.models import AppUser, SsoProvider, Tenant, UserIdentity, UserRole
from vera_core.models.enums import ProviderKind

PASSWORD = "correct horse battery staple"


@dataclass
class LoginWorld:
    tenant_id: UUID
    slug: str
    email: str


@pytest.fixture
async def login_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, LoginWorld]]:
    admin_engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(admin_engine, expire_on_commit=False)
    tenant_id = uuid7()
    suffix = tenant_id.hex[:8]
    slug = f"login-{suffix}"  # a human-readable slug, distinct from the UUID
    email = f"user-{suffix}@example.com"

    async with sessionmaker() as session, session.begin():
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Login test {suffix}", status="active"))
        await session.flush()
        permission_ids = await _seed_permissions(session)
        await _seed_system_roles(session, permission_ids)
        admin_role = (
            await session.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'TENANT_ADMIN'")
            )
        ).scalar_one()
        session.add(
            SsoProvider(
                tenant_id=tenant_id,
                provider_type=ProviderKind.PASSWORD.value,
                display_name="Password",
                enabled=True,
                enforce_mfa=False,
            )
        )
        user = AppUser(
            tenant_id=tenant_id, gcip_uid=None, email=email, name="User", status="active"
        )
        session.add(user)
        await session.flush()
        session.add(
            UserIdentity(
                tenant_id=tenant_id,
                app_user_id=user.id,
                provider_type=ProviderKind.PASSWORD.value,
                provider_subject=email,
                email=email,
                hashed_password=hash_password(PASSWORD),
                mfa_enabled=False,
            )
        )
        session.add(UserRole(tenant_id=tenant_id, app_user_id=user.id, role_id=admin_role))

    settings = Settings(_env_file=None, database_url=rls_database_url)
    app = create_app(
        settings,
        session_store=InMemorySessionStore(),
        kms=LocalDevKMS(master_key=b"a" * 32),
        permission_cache=InMemoryPermissionCache(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, LoginWorld(tenant_id=tenant_id, slug=slug, email=email)

    async with sessionmaker() as session, session.begin():
        for table in (
            "auth_audit_log",
            "audit_log",
            "user_role",
            "role_permission",
            "role",
            "user_identity",
            "app_user",
            "sso_provider",
        ):
            await session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id = :t").bindparams(t=tenant_id)
            )
        await session.execute(text("DELETE FROM tenant WHERE id = :t").bindparams(t=tenant_id))
    await admin_engine.dispose()


def _base(world: LoginWorld) -> str:
    # The URL carries the human-readable slug, resolved to the tenant id server-side.
    return f"/api/v1/tenants/{world.slug}"


async def test_login_success_issues_usable_session(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    resp = await client.post(
        f"{_base(world)}/auth/login", json={"email": world.email, "password": PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["error_code"] is None
    assert body["data"]["mfa"] == "none"
    token = body["data"]["session_token"]
    assert token
    # Every response echoes a correlation id (server-generated when absent).
    assert resp.headers["X-Request-Id"]

    protected = await client.get("/api/v1/calls", headers={"Authorization": f"Bearer {token}"})
    assert protected.status_code == 200
    assert protected.json()["status"] == "SUCCESS"


async def test_me_hydrates_session(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    token = (
        await client.post(
            f"{_base(world)}/auth/login", json={"email": world.email, "password": PASSWORD}
        )
    ).json()["data"]["session_token"]

    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    # Per-user auth data must not be cached by the browser/proxy.
    assert resp.headers["Cache-Control"] == "no-store"
    data = resp.json()["data"]
    assert data["email"] == world.email
    assert data["name"] == "User"  # the seeded display name
    assert data["account_type"] == "tenant"
    assert data["tenant_slug"] == world.slug
    assert "TENANT_ADMIN" in data["roles"]
    assert len(data["permissions"]) > 0
    # Session-timeout config the frontend configures its idle manager from (no more
    # hardcoded/drifting constants). Idle window is the config TTL; the absolute
    # remaining counts down from the freshly minted cap.
    assert data["login_idle_timeout_seconds"] == 3600
    assert 0 < data["login_absolute_remaining_seconds"] <= 10 * 3600


async def test_me_requires_authentication(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, _world = login_world
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


async def test_wrong_password_is_401(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    resp = await client.post(
        f"{_base(world)}/auth/login", json={"email": world.email, "password": "wrong"}
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["status"] == "FAIL"
    assert body["error_code"] == "UNAUTHORIZED"
    assert body["data"] is None


async def test_request_id_is_echoed_when_supplied(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    rid = "client-supplied-correlation-id"
    resp = await client.post(
        f"{_base(world)}/auth/login",
        json={"email": world.email, "password": PASSWORD},
        headers={"X-Request-Id": rid},
    )
    assert resp.headers["X-Request-Id"] == rid


async def test_unknown_email_is_401(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    resp = await client.post(
        f"{_base(world)}/auth/login", json={"email": "ghost@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 401


async def test_unknown_slug_is_401(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, _ = login_world
    # A slug that resolves to no tenant — login must be refused with the uniform 401,
    # not 500 and not a different shape that would let a caller enumerate tenants.
    resp = await client.post(
        "/api/v1/tenants/no-such-tenant/auth/login",
        json={"email": "x@example.com", "password": PASSWORD},
    )
    assert resp.status_code == 401


async def test_malformed_slug_is_401(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, _ = login_world
    # A slug that fails the format check resolves to None before any DB hit -> 401.
    resp = await client.post(
        "/api/v1/tenants/Not_A_Valid_Slug/auth/login",
        json={"email": "x@example.com", "password": PASSWORD},
    )
    assert resp.status_code == 401


async def test_mfa_enroll_activate_then_challenge_flow(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    base = _base(world)

    token = (
        await client.post(f"{base}/auth/login", json={"email": world.email, "password": PASSWORD})
    ).json()["data"]["session_token"]
    headers = {"Authorization": f"Bearer {token}"}

    enroll = await client.post("/api/v1/auth/mfa/enroll", headers=headers)
    assert enroll.status_code == 200
    uri = enroll.json()["data"]["provisioning_uri"]
    secret = pyotp.parse_uri(uri).secret

    activate = await client.post(
        "/api/v1/auth/mfa/activate", headers=headers, json={"code": pyotp.TOTP(secret).now()}
    )
    assert activate.status_code == 200
    assert len(activate.json()["data"]["recovery_codes"]) == 10

    # Login now demands the second factor.
    challenge_resp = await client.post(
        f"{base}/auth/login", json={"email": world.email, "password": PASSWORD}
    )
    assert challenge_resp.status_code == 200
    challenge_body = challenge_resp.json()["data"]
    assert challenge_body["mfa"] == "verify"
    mfa_token = challenge_body["mfa_token"]
    assert mfa_token
    assert challenge_body["session_token"] is None

    verified = await client.post(
        f"{base}/auth/mfa/verify",
        json={"mfa_token": mfa_token, "code": pyotp.TOTP(secret).now()},
    )
    assert verified.status_code == 200
    session_token = verified.json()["data"]["session_token"]
    protected = await client.get(
        "/api/v1/calls", headers={"Authorization": f"Bearer {session_token}"}
    )
    assert protected.status_code == 200


async def test_logout_invalidates_session(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    token = (
        await client.post(
            f"{_base(world)}/auth/login", json={"email": world.email, "password": PASSWORD}
        )
    ).json()["data"]["session_token"]
    auth = {"Authorization": f"Bearer {token}"}

    assert (await client.get("/api/v1/auth/me", headers=auth)).status_code == 200
    assert (await client.post("/api/v1/auth/logout", headers=auth)).status_code == 200
    # Session is gone — the same token no longer authenticates.
    assert (await client.get("/api/v1/auth/me", headers=auth)).status_code == 401


async def test_mfa_verify_rejects_wrong_code(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    base = _base(world)
    token = (
        await client.post(f"{base}/auth/login", json={"email": world.email, "password": PASSWORD})
    ).json()["data"]["session_token"]
    headers = {"Authorization": f"Bearer {token}"}
    uri = (await client.post("/api/v1/auth/mfa/enroll", headers=headers)).json()["data"][
        "provisioning_uri"
    ]
    secret = pyotp.parse_uri(uri).secret
    await client.post(
        "/api/v1/auth/mfa/activate", headers=headers, json={"code": pyotp.TOTP(secret).now()}
    )

    mfa_token = (
        await client.post(f"{base}/auth/login", json={"email": world.email, "password": PASSWORD})
    ).json()["data"]["mfa_token"]
    bad = await client.post(
        f"{base}/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "000000"}
    )
    assert bad.status_code == 401


async def test_keepalive_extends_session(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    token = (
        await client.post(
            f"{_base(world)}/auth/login", json={"email": world.email, "password": PASSWORD}
        )
    ).json()["data"]["session_token"]

    resp = await client.post(
        "/api/v1/auth/session/keepalive", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["Cache-Control"] == "no-store"
    remaining = resp.json()["data"]["expires_in_seconds"]
    assert isinstance(remaining, int)
    assert 0 < remaining <= 3600  # within the default idle window


async def test_keepalive_requires_authentication(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, _world = login_world
    resp = await client.post("/api/v1/auth/session/keepalive")
    assert resp.status_code == 401


async def test_keepalive_after_logout_is_401(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    token = (
        await client.post(
            f"{_base(world)}/auth/login", json={"email": world.email, "password": PASSWORD}
        )
    ).json()["data"]["session_token"]
    auth = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/auth/logout", headers=auth)
    resp = await client.post("/api/v1/auth/session/keepalive", headers=auth)
    assert resp.status_code == 401


async def _enable_enforce_mfa(client: httpx.AsyncClient, world: LoginWorld) -> None:
    """Log in (no MFA yet) as the seeded TENANT_ADMIN and flip the password provider
    to enforce_mfa via the real toggle endpoint."""
    token = (
        await client.post(
            f"{_base(world)}/auth/login", json={"email": world.email, "password": PASSWORD}
        )
    ).json()["data"]["session_token"]
    resp = await client.patch(
        "/api/v1/auth/providers/password",  # authenticated → tenant from session, no slug
        headers={"Authorization": f"Bearer {token}"},
        json={"enabled": True, "enforce_mfa": True},
    )
    assert resp.status_code == 200, resp.text


async def test_enforce_mfa_first_login_enrollment_wall(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    base = _base(world)
    await _enable_enforce_mfa(client, world)

    # Enforced but not enrolled -> the enrollment wall, NOT a verify challenge.
    login = (
        await client.post(f"{base}/auth/login", json={"email": world.email, "password": PASSWORD})
    ).json()["data"]
    assert login["mfa"] == "enroll"
    assert login["session_token"] is None
    assert login["mfa_token"]
    secret = pyotp.parse_uri(login["provisioning_uri"]).secret

    # Confirm a live code -> logged in AND recovery codes returned, in one step.
    activate = await client.post(
        f"{base}/auth/mfa/enroll-activate",
        json={"mfa_token": login["mfa_token"], "code": pyotp.TOTP(secret).now()},
    )
    assert activate.status_code == 200, activate.text
    data = activate.json()["data"]
    assert len(data["recovery_codes"]) == 10
    session_token = data["session_token"]
    protected = await client.get(
        "/api/v1/calls", headers={"Authorization": f"Bearer {session_token}"}
    )
    assert protected.status_code == 200

    # Now enrolled: the next login is the ordinary verify challenge, not the wall.
    relogin = (
        await client.post(f"{base}/auth/login", json={"email": world.email, "password": PASSWORD})
    ).json()["data"]
    assert relogin["mfa"] == "verify"
    assert relogin["mfa_token"]
    verified = await client.post(
        f"{base}/auth/mfa/verify",
        json={"mfa_token": relogin["mfa_token"], "code": pyotp.TOTP(secret).now()},
    )
    assert verified.status_code == 200


async def test_enforce_mfa_first_login_rejects_bad_code(
    login_world: tuple[httpx.AsyncClient, LoginWorld],
) -> None:
    client, world = login_world
    base = _base(world)
    await _enable_enforce_mfa(client, world)

    login = (
        await client.post(f"{base}/auth/login", json={"email": world.email, "password": PASSWORD})
    ).json()["data"]
    bad = await client.post(
        f"{base}/auth/mfa/enroll-activate",
        json={"mfa_token": login["mfa_token"], "code": "000000"},
    )
    assert bad.status_code == 401

    # A bad code must NOT enroll the user — a fresh login still hits the wall.
    relogin = (
        await client.post(f"{base}/auth/login", json={"email": world.email, "password": PASSWORD})
    ).json()["data"]
    assert relogin["mfa"] == "enroll"
    assert relogin["session_token"] is None
