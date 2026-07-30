"""Process-wide settings, loaded from the environment (12-factor).

Secrets (DB passwords, API keys) are NOT pydantic fields in production — they are
resolved through a SecretProvider (see secrets.py) so Secret Manager + CMEK stays
the single source of truth. The defaults below are local-dev only.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: object) -> object:
    """Accept a comma-separated string for a list field (friendlier than JSON in
    .env); pass anything else through untouched for pydantic to validate."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VERA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["local", "dev", "staging", "prod"] = "local"
    log_level: str = "INFO"

    # Cloud SQL Postgres in prod (via auth proxy / private IP); docker-compose locally.
    database_url: str = "postgresql+asyncpg://vera:vera@localhost:5432/vera"
    db_echo: bool = False

    # Memorystore Redis in prod; docker-compose locally.
    redis_url: str = "redis://localhost:6379/0"

    # Live-transcript Redis stream lifetime (Voice Lab / SSE). The rolling backstop
    # TTL is refreshed on every publish so an abandoned stream self-clears; the end
    # grace TTL lets connected readers drain the `ended` sentinel before it clears.
    # The grace window is also the persistence-finalizer's durability budget: the
    # control plane must consume call.ended and drain the stream before it expires,
    # so it is sized to ride out a control-plane restart, not just an SSE drain.
    transcript_stream_ttl_seconds: int = 3600  # VERA_TRANSCRIPT_STREAM_TTL_SECONDS
    transcript_end_grace_seconds: int = 900  # VERA_TRANSCRIPT_END_GRACE_SECONDS

    # Worker→control-plane event bus (Redis Streams + consumer group). Stream is
    # MAXLEN-trimmed; the consumer blocks for block_ms, reclaims entries a crashed
    # consumer left pending after reclaim_idle_ms, and waits teardown_grace_ms after
    # setting failure metadata before deleting the room (so the browser reads it).
    worker_events_stream_maxlen: int = 10_000  # VERA_WORKER_EVENTS_STREAM_MAXLEN
    worker_events_block_ms: int = 5_000  # VERA_WORKER_EVENTS_BLOCK_MS
    worker_events_reclaim_idle_ms: int = 60_000  # VERA_WORKER_EVENTS_RECLAIM_IDLE_MS
    call_failed_teardown_grace_ms: int = 1_500  # VERA_CALL_FAILED_TEARDOWN_GRACE_MS

    # Post-call re-read (LLM eval). Gemini Flash on Vertex (BAA-covered); the review
    # floor routes low-confidence/unsupported fields to EXCEPTION_REVIEW.
    gemini_flash_model: str = "gemini-2.5-flash"  # VERA_GEMINI_FLASH_MODEL
    vertex_location: str = "us-central1"  # VERA_VERTEX_LOCATION
    post_call_review_floor: int = 70  # VERA_POST_CALL_REVIEW_FLOOR
    post_call_block_ms: int = 5_000  # VERA_POST_CALL_BLOCK_MS
    post_call_reclaim_idle_ms: int = 60_000  # VERA_POST_CALL_RECLAIM_IDLE_MS
    # Pipeline sweeper: reconciles stuck calls (worker crash / lost event) and
    # wakes the dispatcher on a timer (working-hours reopen, queue expiry).
    pipeline_sweep_interval_seconds: int = 60  # VERA_PIPELINE_SWEEP_INTERVAL_SECONDS
    # A non-terminal call younger than the grace window is never touched — protects
    # the create→dial gap and normal-end races with the consumer.
    call_stuck_grace_seconds: int = 300  # VERA_CALL_STUCK_GRACE_SECONDS
    # Hard cap: a non-terminal call older than this gets its room deleted and is
    # failed even if the room is still alive (wedged worker session). Payer calls
    # with long holds run long — keep this generous.
    call_max_duration_seconds: int = 3 * 3600  # VERA_CALL_MAX_DURATION_SECONDS
    # Feature gate for the lifecycle's "system auto-retry: low completion" edge
    # (AI_PROCESSING → IN_QUEUE). OFF until post-call answer extraction exists:
    # nothing raises completion_pct between calls today, so a retry would redial
    # up to max_retries times to no benefit. When off, every completed call goes
    # to EXCEPTION_REVIEW.
    form_auto_retry_enabled: bool = False  # VERA_FORM_AUTO_RETRY_ENABLED

    gcp_project: str | None = None

    # --- KMS ------------------------------------------------------------------
    # Full Cloud KMS resource path for production MFA envelope encryption:
    #   projects/{project}/locations/{location}/keyRings/{ring}/cryptoKeys/{key}
    # Unset → LocalDevKMS (requires LOCAL_KMS_MASTER_KEY env var).
    # Set   → GCPCloudKMS (requires Workload Identity or GOOGLE_APPLICATION_CREDENTIALS).
    kms_key_name: str | None = None

    # --- auth -------------------------------------------------------------
    # Login mints an opaque, Redis-backed session token (no client-issued JWTs).
    # The provider used at login is per-tenant (sso_provider table), not a global
    # switch. Sessions are short-lived for HIPAA auto-logoff; revocation is a
    # Redis DEL. The MFA challenge is the brief window between password success
    # and the second factor.
    # `session_ttl_seconds` is the idle window (slid by /auth/session/keepalive);
    # default 15 min, overridable via VERA_SESSION_TTL_SECONDS.
    session_ttl_seconds: int = 15 * 60
    mfa_challenge_ttl_seconds: int = 300
    # First-login enrollment window for a platform operator: unenrolled login only issues
    # a QR within this many seconds of the operator's creation, so a leaked bootstrap
    # password can't bind a second factor long after setup (ADR-0006 §D).
    platform_enroll_window_seconds: int = 30 * 60
    # Hard ceiling on total session lifetime regardless of activity. `session_ttl_seconds`
    # is the idle window (slid by /auth/session/keepalive); this is the absolute max set
    # once at login and never extended. Subject to compliance sign-off.
    session_absolute_max_seconds: int = 10 * 3600

    # User onboarding by invite. The invite token is a single-use, time-boxed bearer
    # credential held only as a hash in Redis (auth/invitations.py); the link delivers
    # the raw token (emailed and/or copied out-of-band by an admin). Carries no PHI —
    # invitees are workforce members. `frontend_base_url` builds the accept link.
    invite_ttl_seconds: int = 72 * 3600

    # Self-service password reset: token far shorter-lived than the 72 h invite
    # (live recovery, not scheduled onboarding); over-limit is a silent generic 200.
    password_reset_ttl_seconds: int = 3600
    password_reset_rate_limit: int = 3
    password_reset_rate_limit_window_seconds: int = 15 * 60

    # --- email (invites + password resets) ----------------------------------
    # Deployed environments send via the Twilio Email API, authenticated with the
    # same Twilio account as outbound SIP (auth token via SecretProvider, never a
    # setting). Setting the account SID selects it; unset falls back to the local
    # msztolcman/sendria SMTP sandbox (docker-compose: SMTP 1025, captured mail at
    # http://localhost:1080). `email_from` must be a Twilio-verified sender.
    twilio_account_sid: str | None = None
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    email_from: str = "no-reply@vera.local"

    # Public origin of the **frontend** SPA — the invite accept link points the
    # browser at the React app (route /tenants/{slug}/accept-invite), NOT at this
    # API. Default is the Vite dev server; override per environment via
    # VERA_FRONTEND_BASE_URL (e.g. the deployed frontend host). No trailing slash.
    frontend_base_url: str = "http://localhost:5173"

    # In-flight lock TTL for an Idempotency-Key (Redis). The first mutating request
    # with a given key claims the lock; a concurrent retry within this short window
    # is rejected 409. It is the request horizon, not a retention window — durable
    # de-dup of late retries is a UNIQUE constraint on the resource, not this lock.
    idempotency_lock_ttl_seconds: int = 30

    # --- observability ------------------------------------------------------
    # Self-hosted Langfuse OTLP endpoint (e.g. https://langfuse.internal). When
    # unset, tracing is a no-op. Langfuse is observability ONLY — the
    # compliance audit trail is the audit_log table, never this.
    langfuse_host: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    otel_service_name: str = "vera"

    # --- livekit ------------------------------------------------------------
    # LiveKit server URL (ws:// for local dev, wss:// in prod).
    # Unset → `build_livekit_gateway` raises ValueError.
    livekit_url: str | None = None

    # Explicit-dispatch agent name. The control plane dispatches jobs to this name and
    # the worker registers under it — they MUST match for a job to route. Default
    # "vera-agent" (used by dev/prod). Override per-environment (set the SAME value on
    # both the control plane and the worker) to isolate a worker pool that shares one
    # LiveKit project — e.g. `VERA_LIVEKIT_AGENT_NAME=vera-agent-local` on a laptop that
    # points at the shared Cloud project, so local dispatches don't land on a deployed worker.
    livekit_agent_name: str = "vera-agent"  # VERA_LIVEKIT_AGENT_NAME

    # --- live-call summary (control plane) -----------------------------------
    # Fault-tolerant summarizer chain, "provider:model" selectors resolved by
    # vera_core.llm (google = Vertex Gemini; openai = GPT under the OpenAI BAA).
    summary_primary_model: str = "google:gemini-3.1-flash-lite"  # VERA_SUMMARY_PRIMARY_MODEL
    summary_fallback_models: list[str] = ["openai:gpt-5.4-mini"]  # VERA_SUMMARY_FALLBACK_MODELS
    summary_attempt_timeout_seconds: float = 8.0  # VERA_SUMMARY_ATTEMPT_TIMEOUT_SECONDS
    # Short cache so tab-flipping supervisors reuse one summary; staleness cap.
    summary_cache_ttl_seconds: int = 5  # VERA_SUMMARY_CACHE_TTL_SECONDS
    # Overall request budget for the summarizer chain (cache + fallback attempts);
    # bounds the worst-case wait before the endpoint gives up and returns 503.
    summary_total_timeout_seconds: float = 20.0  # VERA_SUMMARY_TOTAL_TIMEOUT_SECONDS

    # --- voice cascade (agent worker) -----------------------------------------
    # The live voice cascade's LLM stage — Deepgram(Flux) -> Gemini -> Cartesia
    # (agent_worker/cascade.py). A platform SUPER_ADMIN can override this per-call at
    # runtime (voice_model_config table, platform/llm-config endpoints); this is only
    # the fallback when no override is active. Deliberately its own setting — not
    # shared with any other model config (summary/observer/health chains above, or the
    # post-call gemini_flash_model below): those tune unrelated, out-of-pipeline LLM
    # calls and must be free to change independently of what the live cascade uses.
    voice_llm_default_model: str = "gemini-2.5-flash"  # VERA_VOICE_LLM_DEFAULT_MODEL

    # --- observer answer extraction (agent worker) ---------------------------
    observer_extract_primary_model: str = "google:gemini-3.5-flash"
    observer_extract_fallback_models: list[str] = ["openai:gpt-5.4-mini"]
    observer_extract_attempt_timeout_seconds: float = 8.0

    # --- coaching mode (control plane) ---------------------------------------
    # Shared rolling-window cap on coaching + whisper-transcribe actions PER CALL
    # (one counter, not per supervisor) — any number of authorized supervisors
    # coaching the same call draw from it, so a runaway client can't flood Vera's
    # context or the whisper STT provider.
    coaching_rate_limit_per_minute: int = 15  # VERA_COACHING_RATE_LIMIT_PER_MINUTE
    coaching_rate_limit_window_seconds: int = 60  # VERA_COACHING_RATE_LIMIT_WINDOW_SECONDS
    # Fault-tolerant whisper-transcribe chain (vera_core.stt.ResilientSTT), same
    # "provider:model" selector shape as the summarizer. AssemblyAI has no API key
    # provisioned yet — its factory exists but fails at construction and is
    # dropped with a warning until ASSEMBLYAI_API_KEY is added; Deepgram alone is
    # expected to serve every whisper request until then.
    whisper_stt_primary_model: str = "deepgram:flux-general-en"  # VERA_WHISPER_STT_PRIMARY_MODEL
    whisper_stt_fallback_models: list[str] = ["assemblyai:best"]  # VERA_WHISPER_STT_FALLBACK_MODELS

    @field_validator(
        "summary_fallback_models",
        "observer_extract_fallback_models",
        "whisper_stt_fallback_models",
        mode="before",
    )
    @classmethod
    def _split_fallback_models(cls, value: object) -> object:
        return _split_csv(value)

    # --- call-health observer (agent worker) --------------------------------
    # Fault-tolerant analyzer chain, same "provider:model" selector format as the
    # summary chain; runs INSIDE the agent worker as a per-call background task.
    health_primary_model: str = "google:gemini-3.1-flash-lite"  # VERA_HEALTH_PRIMARY_MODEL
    health_fallback_models: list[str] = ["openai:gpt-5.4-mini"]  # VERA_HEALTH_FALLBACK_MODELS
    health_attempt_timeout_seconds: float = 8.0  # VERA_HEALTH_ATTEMPT_TIMEOUT_SECONDS
    # A completed user turn triggers an analysis, at most one in flight and at
    # least this many seconds apart (silence triggers nothing).
    health_min_interval_seconds: float = 15.0  # VERA_HEALTH_MIN_INTERVAL_SECONDS
    # Cold-start gate: no analysis until this many user turns exist.
    health_min_user_turns: int = 2  # VERA_HEALTH_MIN_USER_TURNS
    # Transcript window cap (chunked re-anchoring — see vera_core.call_health).
    health_max_turns: int = 60  # VERA_HEALTH_MAX_TURNS

    @field_validator("health_fallback_models", mode="before")
    @classmethod
    def _split_health_fallback_models(cls, value: object) -> object:
        return _split_csv(value)

    # --- end-of-call gap pass (agent worker) --------------------------------
    # Before wrapping up a plan-backed call, re-ask required fields that were left
    # unanswered in the tasks the call actually visited. False = go straight to the
    # closing task (the pre-gap-pass behavior).
    gap_pass_enabled: bool = True  # VERA_GAP_PASS_ENABLED

    # --- IVR navigator ------------------------------------------------------
    # Endpointing delays for the IVR-navigator turn handling (agent_worker
    # `ivr_agent.ivr_turn_handling`). min_delay is the key IVR-patience tunable:
    # lower if answers arrive late/out-of-sequence, raise if the bot answers into
    # a mid-prompt pause. Patient by default (a machine pauses mid-readout more than
    # a person does), well above the snappy human-cascade delays.
    ivr_endpointing_min_delay: float = 0.8  # VERA_IVR_ENDPOINTING_MIN_DELAY
    ivr_endpointing_max_delay: float = 1.5  # VERA_IVR_ENDPOINTING_MAX_DELAY

    # --- audit anchoring (WORM bucket) -------------------------------------
    # Periodic anchoring of audit_log chain heads to an object-locked GCS bucket
    # (tamper-PROOF hardening of the tamper-EVIDENT hash chain; devops-todo #10b).
    # Set audit_anchor_bucket → GCSAnchorSink (prod); unset → LocalFilesystemAnchorSink (dev).
    audit_anchor_bucket: str | None = None
    audit_anchor_prefix: str = "audit-anchors"
    audit_anchor_local_dir: str = ".audit-anchors"
    # --- call recording (LiveKit composite egress → GCS) --------------------
    # Unset bucket → recording disabled end-to-end (no egress started, no
    # Recording rows, playback 409s) — mirrors the langfuse_host no-op switch.
    recording_bucket: str | None = None  # VERA_RECORDING_BUCKET
    recording_prefix: str = "recordings"  # VERA_RECORDING_PREFIX
    recording_retention_days_default: int = 90  # VERA_RECORDING_RETENTION_DAYS_DEFAULT
    # Bounded: a misconfigured env var must not mint day-long bearer URLs.
    recording_signed_url_ttl_seconds: int = Field(
        default=600, ge=60, le=3600
    )  # VERA_RECORDING_SIGNED_URL_TTL_SECONDS
    recording_verify_interval_seconds: int = 30  # VERA_RECORDING_VERIFY_INTERVAL_SECONDS
    retention_sweep_interval_seconds: int = 3600  # VERA_RETENTION_SWEEP_INTERVAL_SECONDS
    # An orphan egress (no Recording row) is reaped only once it is older than this,
    # so a just-started recording whose row is still committing is never killed.
    recording_orphan_grace_seconds: int = 300  # VERA_RECORDING_ORPHAN_GRACE_SECONDS
    # --- cors ---------------------------------------------------------------
    # Browser origins allowed to call the API cross-origin (the SPA dev server;
    # the deployed frontend origin(s) in prod). No "*": credentials + PHI require
    # an explicit allowlist. Override with VERA_CORS_ALLOW_ORIGINS as a
    # comma-separated string or a JSON list.
    cors_allow_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        return _split_csv(value)

    @property
    def is_local(self) -> bool:
        return self.env == "local"

    @property
    def call_plan_ttl_seconds(self) -> int:
        return self.call_max_duration_seconds + 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
