"""Application factory and entrypoint for the Vera control plane."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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
from control_plane.livekit_gateway import LiveKitGateway, build_livekit_gateway
from control_plane.recording_jobs import RecordingVerifier
from control_plane.recording_storage import GCSRecordingStorage, RecordingStorage
from control_plane.request_context import RequestIdMiddleware
from control_plane.worker_events import WorkerEventConsumer
from vera_core.audit import (
    AuditSink,
    AuthAuditSink,
    DatabaseAuditWriter,
    DatabaseAuthAuditWriter,
)
from vera_core.config import EnvSecretProvider, SecretProvider, Settings, get_settings
from vera_core.config.kms import KeyManagementService, build_kms
from vera_core.db import create_engine, create_sessionmaker
from vera_core.observability.otel import configure_observability
from vera_core.redis import create_redis
from vera_core.transcript import RedisTranscriptStore, TranscriptService

logger = logging.getLogger("control_plane.main")


def _log_background_exit(task: asyncio.Task[None]) -> None:
    """Surface an unexpected exit of any background task.

    `run()` only returns via cancellation (shutdown) or an uncaught exception; without
    this callback the latter would die silently ("Task exception was never retrieved").
    """
    if not task.cancelled() and task.exception() is not None:
        logger.error("background task exited unexpectedly", exc_info=task.exception())


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
    livekit: LiveKitGateway | None = None,
    secrets: SecretProvider | None = None,
    transcript_service: TranscriptService | None = None,
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
        transcript_redis: Redis | None = None

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
        app.state.secrets = secrets or EnvSecretProvider()
        app.state.livekit = livekit or (
            build_livekit_gateway(settings, app.state.secrets)
            if settings.livekit_url is not None
            else None
        )
        # A DEDICATED Redis client (separate pool) for transcript streaming: a tailing
        # SSE stream holds a connection for its lifetime, so it must not draw from the
        # shared pool that serves session/permission/idempotency Redis (auth DoS risk).
        _transcript_service = transcript_service
        if _transcript_service is None:
            transcript_redis = create_redis(settings.redis_url)
            _transcript_service = TranscriptService(
                RedisTranscriptStore(
                    transcript_redis,
                    ttl_seconds=settings.transcript_stream_ttl_seconds,
                    end_grace_seconds=settings.transcript_end_grace_seconds,
                )
            )
        app.state.transcript_service = _transcript_service
        app.state.audit = audit or DatabaseAuditWriter(sessionmaker)
        app.state.auth_audit = auth_audit or DatabaseAuthAuditWriter(sessionmaker)
        app.state.permission_resolver = PermissionResolver(cache)
        app.state.email_sender = email_sender or SmtpEmailSender(
            host=settings.smtp_host, port=settings.smtp_port, sender=settings.email_from
        )
        app.state.invitation_store = invitation_store or RedisInvitationStore(_redis())

        # Worker→control-plane event consumer. Needs a real LiveKit gateway (to tear
        # rooms down) and a dedicated Redis client (a blocking XREADGROUP pins a
        # connection — same reason the transcript stream gets its own client). Not
        # started when SIP/LiveKit is unconfigured (tests / local without a trunk).
        worker_events_redis: Redis | None = None
        worker_event_task: asyncio.Task[None] | None = None
        if settings.livekit_url is not None and app.state.livekit is not None:
            worker_events_redis = create_redis(settings.redis_url)
            consumer = WorkerEventConsumer(
                worker_events_redis,
                app.state.livekit,
                block_ms=settings.worker_events_block_ms,
                reclaim_idle_ms=settings.worker_events_reclaim_idle_ms,
                teardown_grace_ms=settings.call_failed_teardown_grace_ms,
                sessionmaker=sessionmaker,
                transcripts=_transcript_service,
            )
            worker_event_task = asyncio.create_task(consumer.run())
            worker_event_task.add_done_callback(_log_background_exit)

        # Recording verifier: reconciles PENDING egresses → AVAILABLE (sha256) /
        # FAILED / DISCARDED. Only runs when recording is configured AND LiveKit
        # is available (it queries egress status).
        recording_storage: RecordingStorage | None = None
        verifier_task: asyncio.Task[None] | None = None
        if settings.recording_bucket is not None:
            recording_storage = GCSRecordingStorage()
        app.state.recording_storage = recording_storage
        if recording_storage is not None and app.state.livekit is not None:
            verifier = RecordingVerifier(
                sessionmaker,
                app.state.livekit,
                recording_storage,
                app.state.audit,
                interval_seconds=settings.recording_verify_interval_seconds,
                retention_days_default=settings.recording_retention_days_default,
            )
            verifier_task = asyncio.create_task(verifier.run())
            verifier_task.add_done_callback(_log_background_exit)

        configure_observability(settings)
        yield
        if verifier_task is not None:
            verifier_task.cancel()
            with suppress(asyncio.CancelledError):
                await verifier_task
        if worker_event_task is not None:
            worker_event_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_event_task
        if worker_events_redis is not None:
            await worker_events_redis.aclose()
        if redis is not None:
            await redis.aclose()
        if transcript_redis is not None:
            await transcript_redis.aclose()
        await engine.dispose()

    app = FastAPI(title="Vera Control Plane", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    # Outermost (added last) so it answers the browser preflight OPTIONS and
    # attaches CORS headers to every response, including error envelopes.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )
    app.include_router(v1_router)
    register_exception_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
