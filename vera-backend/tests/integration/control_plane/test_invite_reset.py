"""Integration test for the shared resend/reset helper — needs a real Postgres
since it exercises a real DELETE against a real AppUser/UserIdentity pair."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.auth.invitations import INVITE_NS, InMemoryInvitationStore
from control_plane.auth.invite_reset import reset_and_reissue_invite
from vera_core.db import tenant_session, uuid7
from vera_core.models import AppUser, Tenant, UserIdentity
from vera_core.models.enums import ProviderKind

pytestmark = pytest.mark.anyio


@dataclass
class TenantWorld:
    tenant_id: UUID
    user_id: UUID
    email: str


@pytest.fixture
async def tenant_world(
    database_url: str, rls_database_url: str
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], TenantWorld]]:
    seed_engine = create_async_engine(database_url)
    seed_sm = async_sessionmaker(seed_engine, expire_on_commit=False)
    tenant_id, user_id = uuid7(), uuid7()
    email = "stuck-invitee@test.example"

    async with seed_sm() as s, s.begin():
        s.add(Tenant(id=tenant_id, slug=str(tenant_id), name="Invite Reset Test", status="active"))
        await s.flush()
        s.add(
            AppUser(
                id=user_id,
                tenant_id=tenant_id,
                account_type="tenant",
                email=email,
                name="Stuck Invitee",
                status="invited",
            )
        )

    rls_engine = create_async_engine(rls_database_url)
    rls_sm = async_sessionmaker(rls_engine, expire_on_commit=False)
    yield rls_sm, TenantWorld(tenant_id=tenant_id, user_id=user_id, email=email)

    async with seed_sm() as s, s.begin():
        await s.execute(
            text("DELETE FROM user_identity WHERE app_user_id = :u").bindparams(u=user_id)
        )
        await s.execute(text("DELETE FROM app_user WHERE id = :u").bindparams(u=user_id))
        await s.execute(text("DELETE FROM tenant WHERE id = :t").bindparams(t=tenant_id))
    await rls_engine.dispose()
    await seed_engine.dispose()


async def test_reset_and_reissue_deletes_stale_identity_and_mints_fresh_token(
    tenant_world: tuple[async_sessionmaker[AsyncSession], TenantWorld],
) -> None:
    rls_sm, world = tenant_world
    invites = InMemoryInvitationStore()

    async with tenant_session(rls_sm, world.tenant_id) as session:
        user = (
            await session.execute(select(AppUser).where(AppUser.id == world.user_id))
        ).scalar_one()
        session.add(
            UserIdentity(
                tenant_id=world.tenant_id,
                app_user_id=user.id,
                provider_type=ProviderKind.PASSWORD.value,
                provider_subject=user.email,
                email=user.email,
                hashed_password="stale-hash",
                mfa_enabled=False,
            )
        )
        await session.flush()

        token = await reset_and_reissue_invite(
            session, invites, namespace=INVITE_NS, app_user=user, ttl_seconds=60
        )

        remaining = (
            (await session.execute(select(UserIdentity).where(UserIdentity.app_user_id == user.id)))
            .scalars()
            .all()
        )
        assert remaining == []

    fetched = await invites.get(INVITE_NS, token)
    assert fetched is not None
    assert fetched.app_user_id == world.user_id
    assert fetched.email == world.email
