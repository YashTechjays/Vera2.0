"""Process-wide settings, loaded from the environment (12-factor).

Secrets (DB passwords, API keys) are NOT pydantic fields in production — they are
resolved through a SecretProvider (see secrets.py) so Secret Manager + CMEK stays
the single source of truth. The defaults below are local-dev only.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    session_ttl_seconds: int = 3600
    mfa_challenge_ttl_seconds: int = 300
    # Hard ceiling on total session lifetime regardless of activity. `session_ttl_seconds`
    # is the idle window (slid by /auth/session/keepalive); this is the absolute max set
    # once at login and never extended. Subject to compliance sign-off.
    session_absolute_max_seconds: int = 10 * 3600

    # User onboarding by invite. The invite token is a single-use, time-boxed bearer
    # credential held only as a hash in Redis (auth/invitations.py); the link delivers
    # the raw token (emailed and/or copied out-of-band by an admin). Carries no PHI —
    # invitees are workforce members. `app_base_url` builds the accept link.
    invite_ttl_seconds: int = 72 * 3600

    # --- email (invites) ---------------------------------------------------
    # Local dev uses the msztolcman/sendria SMTP sandbox (docker-compose): SMTP on
    # 1025, captured mail viewable at http://localhost:1080. Production points these
    # at the real relay. No auth/TLS knobs here yet — added with the prod relay.
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    email_from: str = "no-reply@vera.local"
    app_base_url: str = "http://localhost:8000"

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

    @property
    def is_local(self) -> bool:
        return self.env == "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
