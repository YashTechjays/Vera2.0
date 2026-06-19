"""Application factory and entrypoint for the Vera control plane."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from control_plane.api.v1 import router as v1_router
from control_plane.auth.identity import TokenVerifier
from control_plane.auth.invitations import InvitationStore, RedisInvitationStore
from control_plane.auth.permission_cache import PermissionCache, RedisPermissionCache
from control_plane.auth.rbac import PermissionResolver
from control_plane.auth.session import RedisSessionStore, SessionStore, SessionVerifier
from control_plane.email import EmailSender, SmtpEmailSender
from control_plane.exceptions import register_exception_handlers
from control_plane.idempotency import IdempotencyStore, RedisIdempotencyStore
from control_plane.request_context import RequestIdMiddleware
from vera_core.audit import (
    AuditSink,
    AuthAuditSink,
    DatabaseAuditWriter,
    DatabaseAuthAuditWriter,
)
from vera_core.config import Settings, get_settings
from vera_core.config.kms import KeyManagementService, build_kms
from vera_core.db import create_engine, create_sessionmaker
from vera_core.redis import create_redis


def create_app(
    settings: Settings | None = None,
    *,
    token_verifier: TokenVerifier | None = None,
    audit: AuditSink | None = None,
    auth_audit: AuthAuditSink | None = None,
    permission_cache: PermissionCache | None = None,
    session_store: SessionStore | None = None,
    kms: KeyManagementService | None = None,
    idempotency: IdempotencyStore | None = None,
    email_sender: EmailSender | None = None,
    invitation_store: InvitationStore | None = None,
) -> FastAPI:
    """Keyword overrides exist for tests; production wiring comes from Settings.

    The only request-time credential is an opaque session token resolved by
    SessionVerifier — there is no GCIP/static verifier switch anymore. Login
    (provider-driven, per tenant) mints the session; see auth/session.py.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        engine = create_engine(settings)
        sessionmaker = create_sessionmaker(engine)
        app.state.engine = engine
        app.state.sessionmaker = sessionmaker
        app.state.settings = settings

        # Build the Redis client lazily — only for the backends we weren't
        # handed (tests inject both and never touch Redis).
        redis: Redis | None = None

        def _redis() -> Redis:
            nonlocal redis
            if redis is None:
                redis = create_redis(settings.redis_url)
            return redis

        store = session_store or RedisSessionStore(_redis())
        cache = permission_cache or RedisPermissionCache(_redis())
        app.state.redis = redis
        app.state.session_store = store
        app.state.idempotency = idempotency or RedisIdempotencyStore(_redis())
        app.state.token_verifier = token_verifier or SessionVerifier(store)
        app.state.kms = kms or build_kms(settings)
        app.state.audit = audit or DatabaseAuditWriter(sessionmaker)
        app.state.auth_audit = auth_audit or DatabaseAuthAuditWriter(sessionmaker)
        app.state.permission_resolver = PermissionResolver(cache)
        app.state.email_sender = email_sender or SmtpEmailSender(
            host=settings.smtp_host, port=settings.smtp_port, sender=settings.email_from
        )
        app.state.invitation_store = invitation_store or RedisInvitationStore(_redis())
        # TODO(vera-2.x): observability — OTel/Langfuse init hooks in here.
        yield
        if redis is not None:
            await redis.aclose()
        await engine.dispose()

    app = FastAPI(title="Vera Control Plane", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(v1_router)
    register_exception_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
