"""Application factory and entrypoint for the Vera control plane."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
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
from control_plane.call_summary import RedisSummaryCache, SummaryCache
from control_plane.dispatch import drain_pending
from control_plane.email import EmailSender, build_email_sender
from control_plane.exceptions import register_exception_handlers
from control_plane.idempotency import IdempotencyStore, RedisIdempotencyStore
from control_plane.livekit_gateway import LiveKitGateway, build_livekit_gateway
from control_plane.llm import VertexLLMClient
from control_plane.pipeline_sweeper import PipelineSweeper
from control_plane.post_call_consumer import PostCallConsumer
from control_plane.rate_limit import (
    CallRateLimiter,
    PasswordResetRateLimiter,
    RedisCallRateLimiter,
    RedisPasswordResetRateLimiter,
)
from control_plane.recording_jobs import RecordingVerifier, RetentionSweeper
from control_plane.recording_storage import GCSRecordingStorage, RecordingStorage
from control_plane.request_context import RequestIdMiddleware
from control_plane.worker_events import WorkerEventConsumer
from vera_core.audit import (
    AuditSink,
    AuthAuditSink,
    DatabaseAuditWriter,
    DatabaseAuthAuditWriter,
)
from vera_core.call_stream import CallStreamService, RedisCallStreamStore
from vera_core.config import EnvSecretProvider, SecretProvider, Settings, get_settings
from vera_core.config.kms import KeyManagementService, build_kms
from vera_core.db import create_engine, create_sessionmaker
from vera_core.events import PostCallJobBus
from vera_core.llm import FallbackOptions, LLMSpec, ResilientLLM
from vera_core.notifications import NotificationService, RedisNotificationStore
from vera_core.observability.otel import configure_observability
from vera_core.plan_store import CallPlanService, RedisCallPlanStore
from vera_core.redis import create_redis
from vera_core.services.recordings import recording_config_from
from vera_core.stt import ResilientSTT, STTSpec

logger = logging.getLogger("control_plane.main")


def _log_task_exit(label: str) -> Callable[[asyncio.Task[None]], None]:
    """Build a done-callback that surfaces an unexpected exit of a lifespan
    background task (the worker-event / post-call consumers, the pipeline
    sweeper, the recording verifier / retention sweeper).

    `run()` only returns via cancellation (shutdown) or an uncaught exception; without
    this callback the latter would die silently ("Task exception was never retrieved").
    """

    def _on_done(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("%s exited unexpectedly", label, exc_info=task.exception())

    return _on_done


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    """Cancel a background task on shutdown and await its exit, swallowing the
    expected CancelledError. No-op when the task was never started."""
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


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
    call_stream_service: CallStreamService | None = None,
    call_plan_service: CallPlanService | None = None,
    summary_llm: ResilientLLM | None = None,
    summary_cache: SummaryCache | None = None,
    notification_service: NotificationService | None = None,
    call_rate_limiter: CallRateLimiter | None = None,
    password_reset_rate_limiter: PasswordResetRateLimiter | None = None,
    whisper_stt: ResilientSTT | None = None,
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
        call_stream_redis: Redis | None = None
        notifications_redis: Redis | None = None

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
        # Fault-tolerant summarizer chain. Construction is lazy inside
        # ResilientLLM (no provider client until first use), so this is safe
        # even when the OpenAI key is absent in an env that never summarizes.
        owns_summary_llm = summary_llm is None
        app.state.summary_llm = summary_llm or ResilientLLM(
            LLMSpec.parse(settings.summary_primary_model),
            [LLMSpec.parse(selector) for selector in settings.summary_fallback_models],
            options=FallbackOptions(attempt_timeout=settings.summary_attempt_timeout_seconds),
            secrets=app.state.secrets,
        )
        app.state.summary_cache = summary_cache or RedisSummaryCache(_redis())
        # Coaching + whisper rate limit — one-shot INCR/EXPIRE, no tailing/blocking
        # reads, so the shared pool is fine (same reasoning as call_plans below).
        app.state.call_rate_limiter = call_rate_limiter or RedisCallRateLimiter(
            _redis(),
            limit=settings.coaching_rate_limit_per_minute,
            window_seconds=settings.coaching_rate_limit_window_seconds,
        )
        app.state.password_reset_rate_limiter = (
            password_reset_rate_limiter
            or RedisPasswordResetRateLimiter(
                _redis(),
                limit=settings.password_reset_rate_limit,
                window_seconds=settings.password_reset_rate_limit_window_seconds,
            )
        )
        # Whisper's fault-tolerant STT chain. Construction is lazy inside
        # ResilientSTT (no provider client until first transcribe()), so this is
        # safe even before ASSEMBLYAI_API_KEY exists.
        owns_whisper_stt = whisper_stt is None
        app.state.whisper_stt = whisper_stt or ResilientSTT(
            STTSpec.parse(settings.whisper_stt_primary_model),
            [STTSpec.parse(selector) for selector in settings.whisper_stt_fallback_models],
            secrets=app.state.secrets,
        )
        # A DEDICATED Redis client (separate pool) for call-event streaming: a tailing
        # SSE stream holds a connection for its lifetime, so it must not draw from the
        # shared pool that serves session/permission/idempotency Redis (auth DoS risk).
        # This one stream backs both SSE endpoints (real-call and Voice Lab), the
        # transcript finalizer, the summariser, and the worker's Observer.
        _call_stream_service = call_stream_service
        if _call_stream_service is None:
            call_stream_redis = create_redis(settings.redis_url)
            _call_stream_service = CallStreamService(
                RedisCallStreamStore(
                    call_stream_redis,
                    ttl_seconds=settings.transcript_stream_ttl_seconds,
                    end_grace_seconds=settings.transcript_end_grace_seconds,
                )
            )
        app.state.call_stream_service = _call_stream_service
        # User-scoped realtime notifications (intervention alerts). Same
        # dedicated-client reasoning as the SSE streams above: every connected
        # user pins a blocking XREAD, which must not starve the shared pool.
        _notifications = notification_service
        if _notifications is None:
            notifications_redis = create_redis(settings.redis_url)
            _notifications = NotificationService(RedisNotificationStore(notifications_redis))
        app.state.notifications = _notifications
        # Call-plan staging (dispatch writes, worker reads). One-shot SET/GET —
        # no tailing/blocking reads — so the shared pool is fine.
        _call_plans = call_plan_service or CallPlanService(
            RedisCallPlanStore(_redis(), ttl_seconds=settings.call_plan_ttl_seconds)
        )
        app.state.call_plans = _call_plans
        app.state.audit = audit or DatabaseAuditWriter(sessionmaker)
        app.state.auth_audit = auth_audit or DatabaseAuthAuditWriter(sessionmaker)
        app.state.permission_resolver = PermissionResolver(cache)
        app.state.email_sender = email_sender or build_email_sender(settings, app.state.secrets)
        app.state.invitation_store = invitation_store or RedisInvitationStore(_redis())

        # Both stream consumers need a real LiveKit gateway (to tear rooms down) and are
        # skipped when SIP/LiveKit is unconfigured (tests / local without a trunk).
        livekit_ready = settings.livekit_url is not None and app.state.livekit is not None

        # Worker→control-plane event consumer. Uses a dedicated Redis client (a blocking
        # XREADGROUP pins a connection — same reason the transcript stream gets its own).
        worker_events_redis: Redis | None = None
        worker_event_task: asyncio.Task[None] | None = None
        # Post-call eval bus: always set so tests can enqueue through it. The
        # worker-events close path only gets it when the eval consumer will run
        # (needs a GCP project for the LLM) — otherwise jobs would pile up
        # undrained and forms would strand in AI_PROCESSING until the sweeper.
        app.state.post_call_bus = PostCallJobBus(_redis())
        post_call_eval_ready = settings.gcp_project is not None
        # Derived once; None when the bucket is unset (recording disabled) — every
        # consumer (dispatch refill, sweeper wake-up, verifier reap) shares it.
        recording_config = recording_config_from(settings)
        sweeper_task: asyncio.Task[None] | None = None
        if livekit_ready:
            worker_events_redis = create_redis(settings.redis_url)
            consumer = WorkerEventConsumer(
                worker_events_redis,
                app.state.livekit,
                sessionmaker,
                app.state.kms,
                app.state.audit,
                app.state.call_stream_service,
                block_ms=settings.worker_events_block_ms,
                reclaim_idle_ms=settings.worker_events_reclaim_idle_ms,
                teardown_grace_ms=settings.call_failed_teardown_grace_ms,
                form_auto_retry_enabled=settings.form_auto_retry_enabled,
                recording=recording_config,
                call_plans=_call_plans,
                post_call_bus=app.state.post_call_bus if post_call_eval_ready else None,
                notifications=_notifications,
            )
            worker_event_task = asyncio.create_task(consumer.run())
            worker_event_task.add_done_callback(_log_task_exit("worker-event consumer"))

            # Time-based safety net: reconciles stuck calls (crashed worker, no
            # call.ended) and wakes the dispatcher on a timer (working-hours
            # reopen, queue expiry). Same gate as the consumer — needs a real
            # LiveKit gateway to probe/tear down rooms.
            sweeper = PipelineSweeper(
                sessionmaker,
                app.state.livekit,
                app.state.kms,
                app.state.audit,
                app.state.call_stream_service,
                interval_s=settings.pipeline_sweep_interval_seconds,
                stuck_grace_s=settings.call_stuck_grace_seconds,
                max_call_duration_s=settings.call_max_duration_seconds,
                form_auto_retry_enabled=settings.form_auto_retry_enabled,
                recording=recording_config,
                call_plans=_call_plans,
            )
            sweeper_task = asyncio.create_task(sweeper.run())
            sweeper_task.add_done_callback(_log_task_exit("pipeline sweeper"))

        # Recording verifier: reconciles PENDING egresses → AVAILABLE (sha256) /
        # FAILED / DISCARDED. Only runs when recording is configured AND LiveKit
        # is available (it queries egress status).
        recording_storage: RecordingStorage | None = None
        verifier_task: asyncio.Task[None] | None = None
        retention_sweeper_task: asyncio.Task[None] | None = None
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
                recording_config=recording_config,
                orphan_grace_seconds=settings.recording_orphan_grace_seconds,
            )
            verifier_task = asyncio.create_task(verifier.run())
            verifier_task.add_done_callback(_log_task_exit("recording verifier"))
        # Retention sweeper: deletes recordings past retention_until with before/after
        # audit snapshots. Needs storage but NOT LiveKit (no egress queries).
        if recording_storage is not None:
            retention_sweeper = RetentionSweeper(
                sessionmaker,
                recording_storage,
                app.state.audit,
                interval_seconds=settings.retention_sweep_interval_seconds,
            )
            retention_sweeper_task = asyncio.create_task(retention_sweeper.run())
            retention_sweeper_task.add_done_callback(_log_task_exit("retention sweeper"))

        post_call_redis: Redis | None = None
        post_call_task: asyncio.Task[None] | None = None
        if livekit_ready and settings.gcp_project is not None:
            post_call_redis = create_redis(settings.redis_url)
            llm = VertexLLMClient(
                project=settings.gcp_project,
                location=settings.vertex_location,
                model=settings.gemini_flash_model,
                timeout_ms=settings.post_call_llm_timeout_ms,
            )
            post_call_consumer = PostCallConsumer(
                post_call_redis,
                sessionmaker,
                _call_stream_service,
                llm,
                app.state.audit,
                app.state.livekit,
                kms=app.state.kms,
                recording=recording_config,
                plan_service=_call_plans,
                block_ms=settings.post_call_block_ms,
                reclaim_idle_ms=settings.post_call_reclaim_idle_ms,
                review_floor=settings.post_call_review_floor,
                auto_retry_enabled=settings.form_auto_retry_enabled,
            )
            post_call_task = asyncio.create_task(post_call_consumer.run())
            post_call_task.add_done_callback(_log_task_exit("post-call consumer"))

        configure_observability(settings)
        yield
        # Stop background loops in reverse start order before closing their clients.
        await _cancel_task(post_call_task)
        await _cancel_task(retention_sweeper_task)
        await _cancel_task(verifier_task)
        await _cancel_task(sweeper_task)
        await _cancel_task(worker_event_task)
        if owns_summary_llm:
            await app.state.summary_llm.aclose()
        if owns_whisper_stt:
            await app.state.whisper_stt.aclose()
        # Detached dispatch tasks (post-commit enqueue / consumer refill) must finish
        # before the engine goes away — they hold their own sessions off this engine.
        await drain_pending()
        if post_call_redis is not None:
            await post_call_redis.aclose()
        if worker_events_redis is not None:
            await worker_events_redis.aclose()
        if redis is not None:
            await redis.aclose()
        if call_stream_redis is not None:
            await call_stream_redis.aclose()
        if notifications_redis is not None:
            await notifications_redis.aclose()
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
