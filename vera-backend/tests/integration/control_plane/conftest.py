"""A real RBAC world (live Postgres, RLS-enforcing connection) and an app whose
sessions are minted into an in-memory store — everything else is production
wiring. Sessions stand in for a completed login so these tests exercise the
verify path (SessionVerifier -> tenant_guard -> require) without re-running the
password/MFA dance, which has its own tests."""

from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from datetime import timedelta
from typing import NamedTuple
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.auth.invitations import InMemoryInvitationStore
from control_plane.auth.permission_cache import InMemoryPermissionCache
from control_plane.auth.session import InMemorySessionStore, SessionData
from control_plane.dispatch import drain_pending
from control_plane.email import InMemoryEmailSender
from control_plane.livekit_gateway import (
    LiveKitGateway,
    LiveKitUnavailable,
    OutboundDialError,
    RoomParticipant,
)
from control_plane.main import create_app
from control_plane.rate_limit import InMemoryPasswordResetRateLimiter
from scripts.seed import _seed_permissions, _seed_system_roles
from vera_core.call_stream import CallStreamEvent, CallStreamService
from vera_core.config import EnvSecretProvider, Settings
from vera_core.config.kms import KeyManagementService, LocalDevKMS
from vera_core.db import uuid7
from vera_core.db.rls import tenant_session
from vera_core.events import PostCallJob
from vera_core.integrations.credentials import seal_credentials
from vera_core.models import (
    AppUser,
    Call,
    Integration,
    IntegrationType,
    Role,
    RolePermission,
    Tenant,
    UserRole,
)

_LONG_TTL = 3600


class MintedToken(NamedTuple):
    """One mint_join_token call. A NamedTuple so older tests' positional
    assertions (minted[-1][2] is the can_publish flag) keep working."""

    room: str
    identity: str
    can_publish: bool
    name: str | None
    attributes: dict[str, str] | None
    ttl: timedelta


class FakePostCallBus:
    """Records emitted PostCallJobs for test assertions; no Redis required."""

    def __init__(self) -> None:
        self.emitted: list[PostCallJob] = []

    async def emit(self, job: PostCallJob) -> None:
        self.emitted.append(job)

    async def ensure_group(self) -> None:
        pass


class FakeLiveKit(LiveKitGateway):
    """Minimal LiveKitGateway stand-in: records created rooms and mints a
    deterministic token so tests can assert without a real LiveKit server."""

    def __init__(self) -> None:
        # Skip the parent __init__ — we don't need real LiveKit credentials.
        self.created: list[str] = []
        self.dispatch_metadata: list[dict[str, object] | None] = []
        self.sip_calls: list[tuple[str, str, str]] = []
        self.deleted: list[str] = []
        self.room_metadata: list[tuple[str, dict[str, object]]] = []
        self.minted: list[MintedToken] = []
        self._url = "ws://fake:7880"
        self._browser_callee_transport = False
        # Test knobs for trunk validation / dial hardening (reset by reset_livekit_knobs):
        self.known_trunks: set[str] = set()  # outbound_trunk_exists membership
        self.lookup_unavailable = False  # outbound_trunk_exists raises LiveKitUnavailable
        self.dial_error = False  # create_sip_participant raises OutboundDialError
        self.room_error: Exception | None = None  # create_call_room raises it
        # room -> current participants; rooms absent from the map are "gone"
        # (room_participants returns None), mirroring LiveKit.
        self.participants: dict[str, list[RoomParticipant]] = {}

    async def room_participants(self, room_name: str) -> list[RoomParticipant] | None:
        return self.participants.get(room_name)

    async def create_call_room(
        self, room_name: str, metadata: dict[str, object] | None = None
    ) -> None:
        if self.room_error is not None:
            raise self.room_error
        self.created.append(room_name)
        self.dispatch_metadata.append(metadata)

    async def outbound_trunk_exists(self, trunk_id: str) -> bool:
        if self.lookup_unavailable:
            raise LiveKitUnavailable("fake LiveKit is unreachable")
        return trunk_id in self.known_trunks

    async def create_sip_participant(
        self, room_name: str, phone_number: str, trunk_id: str
    ) -> None:
        if self.dial_error:
            raise OutboundDialError("fake provider rejected the call")
        self.sip_calls.append((room_name, phone_number, trunk_id))

    async def delete_room(self, room_name: str) -> None:
        self.deleted.append(room_name)

    async def set_room_metadata(self, room_name: str, metadata: dict[str, object]) -> None:
        self.room_metadata.append((room_name, metadata))

    def mint_join_token(
        self,
        room_name: str,
        identity: str,
        *,
        can_publish: bool = True,
        name: str | None = None,
        attributes: dict[str, str] | None = None,
        ttl: timedelta = timedelta(minutes=5),
    ) -> str:
        self.minted.append(MintedToken(room_name, identity, can_publish, name, attributes, ttl))
        return f"faketoken:{room_name}:{identity}"


@pytest.fixture(scope="session")
def fake_livekit() -> FakeLiveKit:
    return FakeLiveKit()


@pytest.fixture(scope="session")
def fake_post_call_bus() -> FakePostCallBus:
    return FakePostCallBus()


@pytest.fixture(autouse=True)
def reset_fake_bus(fake_post_call_bus: FakePostCallBus) -> Iterator[None]:
    """Clear the bus before each test so emissions don't bleed across tests."""
    fake_post_call_bus.emitted.clear()
    yield


@pytest.fixture(autouse=True)
def reset_livekit_knobs(fake_livekit: FakeLiveKit) -> Iterator[None]:
    """The fake is session-scoped; reset its per-test validation/dial knobs before each
    test so state set by one test never leaks into the next."""
    fake_livekit.known_trunks = set()
    fake_livekit.lookup_unavailable = False
    fake_livekit.dial_error = False
    fake_livekit.room_error = None
    fake_livekit.participants = {}
    yield


@pytest.fixture(autouse=True)
async def drain_dispatch_tasks() -> AsyncIterator[None]:
    """Drain detached post-commit dispatch tasks so none crosses a test boundary
    (a stray task would insert rows mid-teardown or pollute the next tenant)."""
    yield
    await drain_pending()


class _MemCallStreamStore:
    """In-memory CallStreamStore fake, keyed by room_name. Session-scoped, so every
    test must use a distinct room_name to avoid preloaded events bleeding across."""

    def __init__(self) -> None:
        self._entries: dict[str, list[tuple[str, CallStreamEvent]]] = {}
        # (room_name, first_entry_deadline_s) per read() — lets tests pin which
        # deadline each SSE branch passes down.
        self.read_deadlines: list[tuple[str, float | None]] = []
        # Every call_status published, surviving delete() — the fake has no reader,
        # so closeout-announce assertions need a durable log.
        self.status_log: list[tuple[str, object]] = []

    async def publish(self, room_name: str, event: CallStreamEvent) -> None:
        entries = self._entries.setdefault(room_name, [])
        entries.append((f"{len(entries)}-0", event))
        if event.type == "call_status":
            self.status_log.append((room_name, event.data.get("status")))

    async def mark_ended(self, room_name: str) -> None:
        self._entries.setdefault(room_name, [])

    async def delete(self, room_name: str) -> None:
        self._entries.pop(room_name, None)

    async def exists(self, room_name: str) -> bool:
        return room_name in self._entries

    async def read(
        self,
        room_name: str,
        *,
        start_id: str = "0",
        first_entry_deadline_s: float | None = None,
    ) -> AsyncIterator[tuple[str, CallStreamEvent]]:
        # This fake replays a fixed snapshot and never blocks; the deadline is only
        # recorded so endpoint tests can assert what was passed.
        self.read_deadlines.append((room_name, first_entry_deadline_s))
        for entry_id, event in list(self._entries.get(room_name, [])):
            yield entry_id, event

    async def read_all(self, room_name: str) -> list[CallStreamEvent]:
        return [event for _entry_id, event in self._entries.get(room_name, [])]


@pytest.fixture(scope="session")
def call_stream_store() -> _MemCallStreamStore:
    return _MemCallStreamStore()


@pytest.fixture(scope="session")
def call_stream_service(call_stream_store: _MemCallStreamStore) -> CallStreamService:
    return CallStreamService(call_stream_store)


class RBACWorld:
    def __init__(self, tenant_id: UUID, other_tenant_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.other_tenant_id = other_tenant_id
        # Filled once the admin/supervisor AppUser rows are created (see rbac_world).
        self.admin_id: UUID = UUID(int=0)
        self.supervisor_id: UUID = UUID(int=0)
        self.virtual_assistant_id: UUID = UUID(int=0)
        self.listener_id: UUID = UUID(int=0)
        # Filled once sessions are minted (see rbac_world).
        self.admin_token = ""
        self.norole_token = ""
        self.ghost_token = ""
        self.supervisor_token = ""
        self.virtual_assistant_token = ""
        # Holds ONLY calls:read (custom tenant role) — can watch, never intervene.
        self.listener_token = ""


async def _mint(store: InMemorySessionStore, *, user_id: UUID, tenant_id: UUID, email: str) -> str:
    # Mint like production (sess + sess_abs companion) so /auth/me can read the
    # absolute-cap TTL; a bare put() would leave no sess_abs and 401 on /me.
    return await store.mint_session(
        SessionData(
            user_id=user_id,
            tenant_id=tenant_id,
            email=email,
            subject=email,
            provider_type="password",
            mfa_passed=True,
            account_type="tenant",
            # slug == UUID string in this world, so the guard's fast path matches the
            # UUID-in-URL test helpers without a DB resolve.
            tenant_slug=str(tenant_id),
        ),
        _LONG_TTL,
        _LONG_TTL,
    )


@pytest.fixture(scope="session")
def session_store() -> InMemorySessionStore:
    return InMemorySessionStore()


@pytest.fixture(scope="session")
async def rbac_world(
    database_url: str, session_store: InMemorySessionStore
) -> AsyncGenerator[RBACWorld]:
    """Two tenants; in the first: default roles, an admin user, and a roleless
    user. Built as superuser (provisioning is privileged), removed afterwards.
    Mints a session token for each persona into the shared in-memory store."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, other_tenant_id = uuid7(), uuid7()
    suffix = tenant_id.hex[:8]
    world = RBACWorld(tenant_id, other_tenant_id)

    async with sessionmaker() as session, session.begin():
        # slug == the tenant UUID string so the existing UUID-in-URL test helpers
        # resolve through the slug column unchanged (a UUID is a valid slug).
        session.add(
            Tenant(id=tenant_id, slug=str(tenant_id), name=f"Authz test {suffix}", status="active")
        )
        session.add(
            Tenant(
                id=other_tenant_id,
                slug=str(other_tenant_id),
                name=f"Authz other {suffix}",
                status="active",
            )
        )
        await session.flush()
        permission_ids = await _seed_permissions(session)
        await _seed_system_roles(session, permission_ids)

        admin_role = (
            await session.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'TENANT_ADMIN'")
            )
        ).scalar_one()
        virtual_assistant_role = (
            await session.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'VIRTUAL_ASSISTANT'")
            )
        ).scalar_one()
        admin = AppUser(
            tenant_id=tenant_id,
            gcip_uid=None,
            email="admin@test.example",
            name="Admin",
            status="active",
        )
        norole = AppUser(
            tenant_id=tenant_id,
            gcip_uid=None,
            email="norole@test.example",
            name="No Role",
            status="active",
        )
        virtual_assistant = AppUser(
            tenant_id=tenant_id,
            gcip_uid=None,
            email="virtual_assistant@test.example",
            name="Virtual Assistant",
            status="active",
        )
        session.add_all([admin, norole, virtual_assistant])
        await session.flush()
        session.add(UserRole(tenant_id=tenant_id, app_user_id=admin.id, role_id=admin_role))
        session.add(
            UserRole(
                tenant_id=tenant_id,
                app_user_id=virtual_assistant.id,
                role_id=virtual_assistant_role,
            )
        )
        admin_id, norole_id, virtual_assistant_id = admin.id, norole.id, virtual_assistant.id
        world.admin_id = admin_id
        world.virtual_assistant_id = virtual_assistant_id

        supervisor_role = (
            await session.execute(
                text("SELECT id FROM role WHERE tenant_id IS NULL AND name = 'SUPERVISOR'")
            )
        ).scalar_one()
        supervisor = AppUser(
            tenant_id=tenant_id,
            gcip_uid=None,
            email="supervisor@test.example",
            name="Supervisor",
            status="active",
        )
        session.add(supervisor)
        await session.flush()
        session.add(
            UserRole(tenant_id=tenant_id, app_user_id=supervisor.id, role_id=supervisor_role)
        )
        supervisor_id = supervisor.id
        world.supervisor_id = supervisor_id

        # Every system role with calls:read now also holds calls:intervene, so a
        # custom LISTENER role is the only way to test read-without-intervene.
        listener_role = Role(tenant_id=tenant_id, name="LISTENER", description="")
        listener = AppUser(
            tenant_id=tenant_id,
            gcip_uid=None,
            email="listener@test.example",
            name="Listener",
            status="active",
        )
        session.add_all([listener_role, listener])
        await session.flush()
        session.add(
            RolePermission(
                tenant_id=tenant_id,
                role_id=listener_role.id,
                permission_id=permission_ids["calls:read"],
            )
        )
        session.add(
            UserRole(tenant_id=tenant_id, app_user_id=listener.id, role_id=listener_role.id)
        )
        listener_id = listener.id

    world.admin_token = await _mint(
        session_store, user_id=admin_id, tenant_id=tenant_id, email="admin@test.example"
    )
    world.norole_token = await _mint(
        session_store, user_id=norole_id, tenant_id=tenant_id, email="norole@test.example"
    )
    world.supervisor_token = await _mint(
        session_store, user_id=supervisor_id, tenant_id=tenant_id, email="supervisor@test.example"
    )
    world.virtual_assistant_token = await _mint(
        session_store,
        user_id=virtual_assistant_id,
        tenant_id=tenant_id,
        email="virtual_assistant@test.example",
    )
    world.listener_id = listener_id
    world.listener_token = await _mint(
        session_store, user_id=listener_id, tenant_id=tenant_id, email="listener@test.example"
    )
    # A valid session whose user_id has no app_user row -> "unknown user" deny.
    world.ghost_token = await _mint(
        session_store, user_id=uuid7(), tenant_id=tenant_id, email="ghost@test.example"
    )

    yield world

    async with sessionmaker() as session, session.begin():
        for table in (
            "audit_log",
            "auth_audit_log",
            "api_key",
            "user_role",
            "role_permission",
            "role",
            "user_identity",
            "app_user",
            "sso_provider",
        ):
            await session.execute(
                text(f"DELETE FROM {table} WHERE tenant_id IN (:a, :b)").bindparams(
                    a=tenant_id, b=other_tenant_id
                )
            )
        await session.execute(
            text("DELETE FROM tenant WHERE id IN (:a, :b)").bindparams(
                a=tenant_id, b=other_tenant_id
            )
        )
    await engine.dispose()


@pytest.fixture(scope="session")
def email_sender() -> InMemoryEmailSender:
    return InMemoryEmailSender()


@pytest.fixture(scope="session")
def invitation_store() -> InMemoryInvitationStore:
    return InMemoryInvitationStore()


@pytest.fixture(scope="session")
async def authz_app(
    rls_database_url: str,
    rbac_world: RBACWorld,
    session_store: InMemorySessionStore,
    email_sender: InMemoryEmailSender,
    invitation_store: InMemoryInvitationStore,
    fake_livekit: FakeLiveKit,
    fake_post_call_bus: FakePostCallBus,
    call_stream_service: CallStreamService,
) -> AsyncGenerator[FastAPI]:
    """The app talks to Postgres as the NON-superuser role: RLS is live under
    the whole request path, including the audit writer. The session store is the
    same instance rbac_world minted tokens into; email + invites are in-memory so
    the invite flow runs without Redis/SMTP.  FakeLiveKit is injected so call
    endpoints exercise the LiveKit seam without a real server."""
    settings = Settings(_env_file=None, database_url=rls_database_url)
    app = create_app(
        settings,
        session_store=session_store,
        kms=LocalDevKMS(master_key=b"a" * 32),
        permission_cache=InMemoryPermissionCache(),
        email_sender=email_sender,
        invitation_store=invitation_store,
        # Generous window so unrelated tests never trip it; the rate-limit test
        # builds its own app with a tight limit.
        password_reset_rate_limiter=InMemoryPasswordResetRateLimiter(
            limit=1000, window_seconds=900
        ),
        livekit=fake_livekit,
        secrets=EnvSecretProvider(),
        call_stream_service=call_stream_service,
    )
    async with app.router.lifespan_context(app):
        app.state.post_call_bus = fake_post_call_bus
        yield app


@pytest.fixture
async def client(authz_app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=authz_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def admin_session(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    async with admin_sessionmaker() as session:
        yield session


TRUNK_INTEGRATION_TYPE = "livekit_outbound_trunk_id"


@pytest.fixture
async def trunk_integration_type(
    admin_sessionmaker: async_sessionmaker[AsyncSession], rbac_world: RBACWorld
) -> AsyncIterator[None]:
    """Find-or-create the `livekit_outbound_trunk_id` catalog type (unseeded in the
    integration DB).

    Teardown is scoped to the test tenant only: the suite shares the local dev DB,
    so a blanket delete would wipe a developer's real credential — NEVER widen it.
    The type row is dropped only if this fixture created it."""
    async with admin_sessionmaker() as session, session.begin():
        created = (
            await session.execute(
                select(IntegrationType).where(IntegrationType.name == TRUNK_INTEGRATION_TYPE)
            )
        ).scalar_one_or_none() is None
        if created:
            session.add(
                IntegrationType(
                    name=TRUNK_INTEGRATION_TYPE, credentials_schema={"trunk_id": "string"}
                )
            )
    try:
        yield
    finally:
        async with admin_sessionmaker() as session, session.begin():
            await session.execute(
                delete(Integration).where(Integration.tenant_id == rbac_world.tenant_id)
            )
            if created:
                await session.execute(
                    delete(IntegrationType).where(IntegrationType.name == TRUNK_INTEGRATION_TYPE)
                )


# The trunk id tests assert against when they check what got dialed.
TEST_TRUNK_ID = "ST_test_trunk"


async def seed_outbound_trunk(
    sessionmaker: async_sessionmaker[AsyncSession], kms: KeyManagementService, tenant_id: UUID
) -> None:
    """Seal a `livekit_outbound_trunk_id` credential for `tenant_id` so any
    outbound-dial seam resolves a trunk from the DB. Requires the
    `trunk_integration_type` fixture."""
    async with sessionmaker() as session, session.begin():
        type_id = (
            await session.execute(
                select(IntegrationType.id).where(IntegrationType.name == TRUNK_INTEGRATION_TYPE)
            )
        ).scalar_one()
        integration = Integration(
            tenant_id=tenant_id,
            integration_type_id=type_id,
            status="active",
        )
        await seal_credentials(
            kms, integration=integration, credentials={"trunk_id": TEST_TRUNK_ID}
        )
        session.add(integration)


async def seed_call(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    form_id: UUID,
    *,
    initiated_by_id: UUID | None = None,
    status: str = "initiated",
    published: bool = False,
) -> UUID:
    """Insert a Call row directly, the way the dispatcher would. Mirrors
    production: `started_at` is set once past the dialing phase."""
    async with tenant_session(sessionmaker, tenant_id) as session:
        call = Call(
            tenant_id=tenant_id,
            form_id=form_id,
            current_status=status,
            initiated_by_id=initiated_by_id,
            published=published,
            started_at=None if status in ("initiated", "ringing", "ivr") else func.now(),
        )
        session.add(call)
        await session.flush()
        return call.id


@pytest.fixture
async def trunk_configured(
    admin_sessionmaker: async_sessionmaker[AsyncSession],
    rbac_world: RBACWorld,
    trunk_integration_type: None,
) -> None:
    """Seal the test tenant's outbound-trunk credential so any dial seam (voice-lab,
    the queueability gate) resolves it. Uses the same LocalDevKMS master key as the app
    under test (`authz_app`), so `get_integration_credentials` can open what we seal."""
    await seed_outbound_trunk(
        admin_sessionmaker, LocalDevKMS(master_key=b"a" * 32), rbac_world.tenant_id
    )
