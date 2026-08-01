"""End-to-end self-service password reset (VR2-104) against live Postgres (RLS
connection). Mirrors test_login_flow's world: a fresh tenant seeded as superuser,
the app on the non-superuser role, in-memory session/invite/email seams so the
test owns their state. The reset email is sent as a detached task — tests drain
via dispatch.drain_pending before asserting on the outbox.
"""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.auth.invitations import InMemoryInvitationStore
from control_plane.auth.password import hash_password
from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.auth.session import InMemorySessionStore
from control_plane.dispatch import drain_pending
from control_plane.email import InMemoryEmailSender
from control_plane.main import create_app
from control_plane.rate_limit import InMemoryPasswordResetRateLimiter
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.config import Settings
from vera_core.config.kms import LocalDevKMS
from vera_core.db import uuid7
from vera_core.models import AppUser, SsoProvider, Tenant, UserIdentity, UserRole
from vera_core.models.enums import ProviderKind

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "brand new passphrase 42"
RATE_LIMIT = 3


@dataclass
class ResetWorld:
    tenant_id: UUID
    slug: str
    email: str
    deactivated_email: str
    invited_email: str
    mfa_email: str
    admin_sessionmaker: async_sessionmaker[AsyncSession]
    email_sender: InMemoryEmailSender


@pytest.fixture
async def reset_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[httpx.AsyncClient, ResetWorld]]:
    admin_engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(admin_engine, expire_on_commit=False)
    tenant_id = uuid7()
    suffix = tenant_id.hex[:8]
    slug = f"pwreset-{suffix}"
    email = f"user-{suffix}@example.com"
    deactivated_email = f"gone-{suffix}@example.com"
    invited_email = f"invited-{suffix}@example.com"
    mfa_email = f"mfa-{suffix}@example.com"

    async with sessionmaker() as session, session.begin():
        session.add(Tenant(id=tenant_id, slug=slug, name=f"Reset test {suffix}", status="active"))
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

        def _user(u_email: str, status: str) -> AppUser:
            return AppUser(
                tenant_id=tenant_id, gcip_uid=None, email=u_email, name="User", status=status
            )

        def _identity(user: AppUser, *, mfa_enabled: bool) -> UserIdentity:
            return UserIdentity(
                tenant_id=tenant_id,
                app_user_id=user.id,
                provider_type=ProviderKind.PASSWORD.value,
                provider_subject=user.email,
                email=user.email,
                hashed_password=hash_password(PASSWORD),
                mfa_enabled=mfa_enabled,
            )

        active = _user(email, "active")
        deactivated = _user(deactivated_email, "deactivated")
        invited = _user(invited_email, "invited")  # never accepted → no password identity
        mfa_user = _user(mfa_email, "active")
        session.add_all([active, deactivated, invited, mfa_user])
        await session.flush()
        session.add_all(
            [
                _identity(active, mfa_enabled=False),
                _identity(deactivated, mfa_enabled=False),
                _identity(mfa_user, mfa_enabled=True),
                UserRole(tenant_id=tenant_id, app_user_id=active.id, role_id=admin_role),
            ]
        )

    email_sender = InMemoryEmailSender()
    settings = Settings(_env_file=None, database_url=rls_database_url)
    app = create_app(
        settings,
        session_store=InMemorySessionStore(),
        kms=LocalDevKMS(master_key=b"a" * 32),
        permission_cache=InMemoryPermissionCache(),
        email_sender=email_sender,
        invitation_store=InMemoryInvitationStore(),
        password_reset_rate_limiter=InMemoryPasswordResetRateLimiter(
            limit=RATE_LIMIT, window_seconds=900
        ),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield (
                client,
                ResetWorld(
                    tenant_id=tenant_id,
                    slug=slug,
                    email=email,
                    deactivated_email=deactivated_email,
                    invited_email=invited_email,
                    mfa_email=mfa_email,
                    admin_sessionmaker=sessionmaker,
                    email_sender=email_sender,
                ),
            )

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


def _base(world: ResetWorld) -> str:
    return f"/api/v1/tenants/{world.slug}"


async def _request_reset(
    client: httpx.AsyncClient, world: ResetWorld, email: str, *, slug: str | None = None
) -> httpx.Response:
    return await client.post(
        f"/api/v1/tenants/{slug or world.slug}/auth/password-reset/request",
        json={"email": email},
    )


async def _token_from_email(world: ResetWorld) -> str:
    await drain_pending()  # the send is a detached task
    assert world.email_sender.sent, "no reset email was sent"
    body = world.email_sender.sent[-1].body
    return body.split("token=", 1)[1].splitlines()[0].strip()


async def _login(
    client: httpx.AsyncClient, world: ResetWorld, email: str, password: str
) -> httpx.Response:
    return await client.post(
        f"{_base(world)}/auth/login", json={"email": email, "password": password}
    )


async def test_request_matches_a_differently_cased_email(
    reset_world: tuple[httpx.AsyncClient, ResetWorld],
) -> None:
    # The stored email is lowercase (fixture); typing it back in a different case
    # must still find the account, not silently land on the ineligible branch.
    client, world = reset_world
    resp = await _request_reset(client, world, world.email.upper())
    assert resp.status_code == 200
    assert world.email_sender.sent, "no reset email was sent for a differently-cased email"


async def test_happy_path_resets_password_and_link_is_single_use(
    reset_world: tuple[httpx.AsyncClient, ResetWorld],
) -> None:
    client, world = reset_world
    resp = await _request_reset(client, world, world.email)
    assert resp.status_code == 200
    token = await _token_from_email(world)
    assert f"/tenants/{world.slug}/reset-password?token=" in world.email_sender.sent[-1].body

    validate = await client.get(
        f"{_base(world)}/auth/password-reset/validate", params={"token": token}
    )
    assert validate.json()["data"]["state"] == "valid"

    confirm = await client.post(
        f"{_base(world)}/auth/password-reset/confirm",
        json={"token": token, "password": NEW_PASSWORD},
    )
    assert confirm.status_code == 200

    assert (await _login(client, world, world.email, PASSWORD)).status_code == 401
    assert (await _login(client, world, world.email, NEW_PASSWORD)).status_code == 200

    # single use: the spent token neither validates nor confirms again
    revalidate = await client.get(
        f"{_base(world)}/auth/password-reset/validate", params={"token": token}
    )
    assert revalidate.json()["data"]["state"] == "invalid"
    reconfirm = await client.post(
        f"{_base(world)}/auth/password-reset/confirm",
        json={"token": token, "password": "another one entirely"},
    )
    assert reconfirm.status_code == 401


async def test_confirm_revokes_every_live_session(
    reset_world: tuple[httpx.AsyncClient, ResetWorld],
) -> None:
    client, world = reset_world
    login = await _login(client, world, world.email, PASSWORD)
    session_token = login.json()["data"]["session_token"]
    headers = {"Authorization": f"Bearer {session_token}"}
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200

    await _request_reset(client, world, world.email)
    token = await _token_from_email(world)
    confirm = await client.post(
        f"{_base(world)}/auth/password-reset/confirm",
        json={"token": token, "password": NEW_PASSWORD},
    )
    assert confirm.status_code == 200

    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401


async def test_request_is_generic_200_with_no_email_for_every_ineligible_case(
    reset_world: tuple[httpx.AsyncClient, ResetWorld],
) -> None:
    client, world = reset_world
    baseline = await _request_reset(client, world, world.email)  # eligible → email
    await drain_pending()
    assert len(world.email_sender.sent) == 1

    for email, slug in (
        (f"nobody-{world.slug}@example.com", None),  # unknown email
        (world.email, f"no-such-{world.slug}"),  # unknown slug
        (world.deactivated_email, None),  # deactivated user
        (world.invited_email, None),  # invited, no password identity yet
    ):
        resp = await _request_reset(client, world, email, slug=slug)
        assert resp.status_code == 200
        assert resp.json() == baseline.json()  # byte-identical envelope — no oracle

    await drain_pending()
    assert len(world.email_sender.sent) == 1  # not one more email


async def test_confirm_with_wrong_tenant_slug_is_401_and_does_not_spend_the_token(
    reset_world: tuple[httpx.AsyncClient, ResetWorld],
) -> None:
    client, world = reset_world
    await _request_reset(client, world, world.email)
    token = await _token_from_email(world)

    wrong = await client.post(
        f"/api/v1/tenants/no-such-{world.slug}/auth/password-reset/confirm",
        json={"token": token, "password": NEW_PASSWORD},
    )
    assert wrong.status_code == 401

    still_valid = await client.get(
        f"{_base(world)}/auth/password-reset/validate", params={"token": token}
    )
    assert still_valid.json()["data"]["state"] == "valid"


async def test_confirm_rejects_over_length_password_without_spending_the_token(
    reset_world: tuple[httpx.AsyncClient, ResetWorld],
) -> None:
    client, world = reset_world
    await _request_reset(client, world, world.email)
    token = await _token_from_email(world)

    resp = await client.post(
        f"{_base(world)}/auth/password-reset/confirm",
        json={"token": token, "password": "x" * 73},
    )
    assert resp.status_code == 400
    assert (await _login(client, world, world.email, PASSWORD)).status_code == 200
    still_valid = await client.get(
        f"{_base(world)}/auth/password-reset/validate", params={"token": token}
    )
    assert still_valid.json()["data"]["state"] == "valid"


async def test_requests_over_the_rate_limit_still_return_200_but_send_nothing(
    reset_world: tuple[httpx.AsyncClient, ResetWorld],
) -> None:
    client, world = reset_world
    for _ in range(RATE_LIMIT):
        assert (await _request_reset(client, world, world.email)).status_code == 200
    over = await _request_reset(client, world, world.email)
    assert over.status_code == 200  # silent — a prober can't see the limit

    await drain_pending()
    assert len(world.email_sender.sent) == RATE_LIMIT


async def test_mfa_enrollment_survives_the_reset(
    reset_world: tuple[httpx.AsyncClient, ResetWorld],
) -> None:
    client, world = reset_world
    await _request_reset(client, world, world.mfa_email)
    token = await _token_from_email(world)
    confirm = await client.post(
        f"{_base(world)}/auth/password-reset/confirm",
        json={"token": token, "password": NEW_PASSWORD},
    )
    assert confirm.status_code == 200

    login = await _login(client, world, world.mfa_email, NEW_PASSWORD)
    assert login.status_code == 200
    assert login.json()["data"]["mfa"] == "verify"  # still enrolled


async def test_audit_rows_are_written_without_the_token(
    reset_world: tuple[httpx.AsyncClient, ResetWorld],
) -> None:
    client, world = reset_world
    await _request_reset(client, world, world.email)
    token = await _token_from_email(world)
    await client.post(
        f"{_base(world)}/auth/password-reset/confirm",
        json={"token": token, "password": NEW_PASSWORD},
    )

    async with world.admin_sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT event_type, metadata::text AS meta FROM auth_audit_log"
                    " WHERE tenant_id = :t AND event_type LIKE 'password_reset%'"
                ).bindparams(t=world.tenant_id)
            )
        ).all()
    events = {r.event_type for r in rows}
    assert events == {"password_reset_requested", "password_reset_completed"}
    for row in rows:
        assert token not in row.meta
        assert "reset-password?" not in row.meta
