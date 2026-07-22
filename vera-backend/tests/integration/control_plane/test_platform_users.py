"""Integration tests for the platform-operator invite/list/deactivate/resend
endpoints, using the World/_mint pattern from test_platform_elevation.py (there is
no shared platform-tier conftest fixture in this repo yet — this file follows the
same local-fixture convention rather than inventing a different one)."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pyotp
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.auth.session import InMemorySessionStore, SessionData
from control_plane.main import create_app
from scripts.seed import _seed_permissions, _seed_system_roles
from tests.integration.control_plane.conftest import RBACWorld
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.models import AppUser, UserRole

# No `pytestmark = pytest.mark.anyio` here: this repo is asyncio-only
# (`asyncio_mode = "auto"` in pyproject.toml) — anyio is a transitive dependency
# only, never a marker to opt into (see test_platform_provisioning.py's docstring
# and repo CLAUDE.md's "asyncio is the single async runtime" rule). Adding it
# activates a second, competing event-loop manager and produces a real "attached
# to a different loop" Redis/asyncpg failure under this suite.


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


@dataclass
class PlatformWorld:
    super_admin_id: UUID
    super_admin_token: str


async def _mint_platform(store: InMemorySessionStore, *, user_id: UUID, email: str) -> str:
    return await store.mint_session(
        SessionData(
            user_id=user_id,
            tenant_id=None,
            email=email,
            subject=email,
            provider_type="password",
            mfa_passed=True,
            account_type="platform",
            tenant_slug=None,
        ),
        3600,
        3600,
    )


@pytest.fixture
async def platform_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, PlatformWorld]]:
    engine = create_async_engine(database_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    super_id = uuid7()

    async with sm() as s, s.begin():
        permission_ids = await _seed_permissions(s)
        await _seed_system_roles(s, permission_ids)
        super_role = (
            await s.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPER_ADMIN'")
            )
        ).scalar_one()
        s.add(
            AppUser(
                id=super_id,
                tenant_id=None,
                account_type="platform",
                email="root@vera.example",
                name="Root",
                status="active",
            )
        )
        await s.flush()
        s.add(UserRole(tenant_id=None, app_user_id=super_id, role_id=super_role))

    store = InMemorySessionStore()
    super_admin_token = await _mint_platform(store, user_id=super_id, email="root@vera.example")

    settings = Settings(_env_file=None, database_url=rls_database_url)
    app = create_app(
        settings,
        session_store=store,
        kms=LocalDevKMS(master_key=b"a" * 32),
        permission_cache=InMemoryPermissionCache(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield (
                client,
                PlatformWorld(super_admin_id=super_id, super_admin_token=super_admin_token),
            )

    # Cleanup covers every platform app_user this test created (invite creates new
    # ones with generated emails, not just the seeded super_id), mirroring the
    # elevation test's per-test teardown scope.
    async with sm() as s, s.begin():
        await s.execute(
            text(
                "DELETE FROM auth_audit_log WHERE app_user_id IN "
                "(SELECT id FROM app_user WHERE account_type = 'platform')"
            )
        )
        await s.execute(
            text(
                "DELETE FROM user_identity WHERE app_user_id IN "
                "(SELECT id FROM app_user WHERE account_type = 'platform')"
            )
        )
        await s.execute(
            text(
                "DELETE FROM user_role WHERE app_user_id IN "
                "(SELECT id FROM app_user WHERE account_type = 'platform')"
            )
        )
        await s.execute(text("DELETE FROM app_user WHERE account_type = 'platform'"))
    await engine.dispose()


async def test_invite_operator_creates_invited_platform_user_with_super_admin(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, pw = platform_world
    resp = await client.post(
        "/api/v1/platform/users/invitations",
        headers={**_auth(pw.super_admin_token), **_idem()},
        json={"email": "new-op@test.example", "name": "New Op", "send_email": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["email"] == "new-op@test.example"
    assert "/platform/accept-invite?token=" in body["invite_url"]


async def test_list_operators_includes_the_new_invite(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, pw = platform_world
    await client.post(
        "/api/v1/platform/users/invitations",
        headers={**_auth(pw.super_admin_token), **_idem()},
        json={"email": "listed-op@test.example", "name": "", "send_email": False},
    )
    resp = await client.get("/api/v1/platform/users", headers=_auth(pw.super_admin_token))
    assert resp.status_code == 200, resp.text
    emails = {row["email"] for row in resp.json()["data"]}
    assert "listed-op@test.example" in emails


async def test_deactivate_operator_succeeds_when_another_active_operator_remains(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, pw = platform_world
    invite = await client.post(
        "/api/v1/platform/users/invitations",
        headers={**_auth(pw.super_admin_token), **_idem()},
        json={"email": "second-op@test.example", "name": "", "send_email": False},
    )
    second_id = invite.json()["data"]["user_id"]
    # Deactivating an invited (not yet active) second operator is fine — the lockout
    # guard only counts ACTIVE operators, and the seeded super_admin is still active.
    resp = await client.post(
        f"/api/v1/platform/users/{second_id}/deactivate",
        headers=_auth(pw.super_admin_token),
    )
    assert resp.status_code == 200, resp.text


async def test_deactivate_operator_blocks_the_last_active_operator(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, pw = platform_world
    resp = await client.post(
        f"/api/v1/platform/users/{pw.super_admin_id}/deactivate",
        headers=_auth(pw.super_admin_token),
    )
    assert resp.status_code == 409, resp.text


async def test_resend_operator_invitation_reissues_a_working_token(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, pw = platform_world
    invite = await client.post(
        "/api/v1/platform/users/invitations",
        headers={**_auth(pw.super_admin_token), **_idem()},
        json={"email": "stuck-op@test.example", "name": "", "send_email": False},
    )
    user_id = invite.json()["data"]["user_id"]
    stale_token = invite.json()["data"]["invite_url"].split("token=", 1)[1]

    resend = await client.post(
        f"/api/v1/platform/users/{user_id}/resend-invitation",
        headers={**_auth(pw.super_admin_token), **_idem()},
    )
    assert resend.status_code == 200, resend.text
    fresh_token = resend.json()["data"]["invite_url"].split("token=", 1)[1]
    assert fresh_token != stale_token

    # As with the tenant resend endpoint (Task 6), the stale token is deliberately
    # NOT invalidated — see that task's note. Resend's contract is "a fresh working
    # link exists," not "the old one is revoked."
    fresh_check = await client.get(
        "/api/v1/platform/auth/invitations/validate", params={"token": fresh_token}
    )
    assert fresh_check.json()["data"]["state"] == "valid"


async def test_full_platform_invite_accept_activate_flow(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    client, pw = platform_world
    invite = await client.post(
        "/api/v1/platform/users/invitations",
        headers={**_auth(pw.super_admin_token), **_idem()},
        json={"email": "flow-op@test.example", "name": "Flow Op", "send_email": False},
    )
    assert invite.status_code == 200, invite.text
    user_id = invite.json()["data"]["user_id"]
    token = invite.json()["data"]["invite_url"].split("token=", 1)[1]

    valid = await client.get("/api/v1/platform/auth/invitations/validate", params={"token": token})
    assert valid.json()["data"]["state"] == "valid"

    accept = await client.post(
        "/api/v1/platform/auth/invitations/accept",
        json={"token": token, "password": "a-strong-password"},
    )
    assert accept.status_code == 200, accept.text
    accept_body = accept.json()["data"]
    assert accept_body["mfa_required"] is True
    assert accept_body["provisioning_uri"] is not None
    mfa_token = accept_body["mfa_token"]

    # Extract the TOTP secret from the provisioning URI to compute a live code.
    secret = pyotp.parse_uri(accept_body["provisioning_uri"]).secret
    code = pyotp.TOTP(secret).now()

    activate = await client.post(
        "/api/v1/platform/auth/invitations/activate-mfa",
        json={"mfa_token": mfa_token, "code": code},
    )
    assert activate.status_code == 200, activate.text

    # The token is single-use — replaying validate now shows "invalid" (accepted).
    revalidate = await client.get(
        "/api/v1/platform/auth/invitations/validate", params={"token": token}
    )
    assert revalidate.json()["data"]["state"] == "invalid"

    listed = await client.get("/api/v1/platform/users", headers=_auth(pw.super_admin_token))
    row = next(r for r in listed.json()["data"] if r["id"] == user_id)
    assert row["status"] == "active"


async def test_platform_accept_invalid_token_is_unauthorized(
    platform_world: tuple[httpx.AsyncClient, PlatformWorld],
) -> None:
    # Only needs a client — reuses platform_world for that, ignoring its persona
    # data, rather than standing up a second app-construction fixture for one test.
    client, _pw = platform_world
    resp = await client.post(
        "/api/v1/platform/auth/invitations/accept",
        json={"token": "not-a-real-token", "password": "whatever-password"},
    )
    assert resp.status_code == 401, resp.text


async def test_elevated_tenant_session_cannot_reach_platform_user_endpoints(
    client: httpx.AsyncClient, rbac_world: "RBACWorld"
) -> None:
    """A tenant admin who elevates into break-glass access must not be able to
    reach the platform-operator endpoints — these require an actual platform
    session (no tenant GUC), not an elevated tenant one."""
    resp = await client.get(
        "/api/v1/platform/users", headers=_auth(rbac_world.admin_token)
    )
    assert resp.status_code in (401, 403), resp.text
