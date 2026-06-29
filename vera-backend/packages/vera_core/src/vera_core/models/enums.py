"""Enumerated value catalogs for the v2 schema.

The repo convention (per the ADR §7 "never free text") is enforced with Postgres
**CHECK constraints** rather than native ENUM types: the values live here once as
`StrEnum`s — reused by application code, by the models' `CheckConstraint`s, and by
migration 0001 — and a CHECK is cheaper to evolve than `ALTER TYPE`. (The one
pre-existing native enum, `audit_log.actor_type`, is left as-is.)
"""

import enum

from sqlalchemy import CheckConstraint


class FormStatus(enum.StrEnum):
    """patient_form record lifecycle (ADR §7, spec §4.3.3)."""

    READY_FOR_PROCESSING = "ready_for_processing"
    IN_QUEUE = "in_queue"
    IN_CALL = "in_call"
    AI_PROCESSING = "ai_processing"
    EXCEPTION_REVIEW = "exception_review"
    COMPLETED = "completed"
    CALL_FAILED = "call_failed"


class CallMode(enum.StrEnum):
    FULL = "full"
    RETRY = "retry"


class CallStatus(enum.StrEnum):
    """Per-call state — distinct from the form's record lifecycle (ADR §7)."""

    INITIATED = "initiated"
    RINGING = "ringing"
    IVR = "ivr"
    ACTIVE = "active"
    WAITING = "waiting"
    CRITICAL = "critical"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"


class CallEventType(enum.StrEnum):
    STATUS = "status"
    PHASE = "phase"
    HEALTH = "health"
    CALLBACK = "callback"


class AnswerSource(enum.StrEnum):
    INTAKE = "intake"
    AI_CALL = "ai_call"
    HUMAN = "human"


class TranscriptSource(enum.StrEnum):
    REP = "rep"
    BOT = "bot"
    SUPERVISOR = "supervisor"


class DisputeActionType(enum.StrEnum):
    ACCEPT = "accept"
    OVERRIDE = "override"
    CORRECT = "correct"


class InterventionType(enum.StrEnum):
    FLAG = "flag"
    COACH = "coach"
    WHISPER = "whisper"
    TAKEOVER = "takeover"


class InterventionCategory(enum.StrEnum):
    """First-class column so the intervention-by-category report is a GROUP BY,
    not a JSONB scan (ADR §6)."""

    REPEATED_QUESTIONS = "repeated_questions"
    HALLUCINATION = "hallucination"
    CONVERSATION_LOOP = "conversation_loop"
    LONG_SILENCE = "long_silence"
    OFF_SCRIPT = "off_script"
    LOW_CONFIDENCE = "low_confidence"
    OTHER = "other"


class ProviderStage(enum.StrEnum):
    STT = "stt"
    LLM = "llm"
    TTS = "tts"


class ExportFormat(enum.StrEnum):
    XLSX = "xlsx"
    PDF = "pdf"


class VersionStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class InsuranceType(enum.StrEnum):
    """The insurance/form family a `form_schema` belongs to. One value for now;
    grows to carrier-level types (aetna/cigna/uhc) with a one-line addition plus a
    CHECK-update migration."""

    INFERTILITY_TREATMENT = "infertility_treatment"


class EvalScope(enum.StrEnum):
    COMPONENT = "component"
    E2E = "e2e"


class ProviderKind(enum.StrEnum):
    """Auth identity provider kinds. `password` is the first-class local provider
    (ADR §3.5.3), not a side channel."""

    GOOGLE_OIDC = "google_oidc"
    SAML = "saml"
    OIDC = "oidc"
    PASSWORD = "password"


class AccountType(enum.StrEnum):
    """App-user account tier (ADR §3.5.9). Governs scoping/home only — confers no
    power by itself; actual privilege comes from RBAC roles + elevation."""

    TENANT = "tenant"
    PLATFORM = "platform"


class AuthEvent(enum.StrEnum):
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    MFA_CHALLENGE = "mfa_challenge"
    USER_INVITED = "user_invited"
    INVITE_ACCEPTED = "invite_accepted"
    USER_DEACTIVATED = "user_deactivated"
    ROLE_CREATED = "role_created"
    ROLE_GRANT = "role_grant"
    ROLE_REVOKE = "role_revoke"
    API_KEY_CREATED = "api_key_created"
    API_KEY_REVOKED = "api_key_revoked"
    TENANT_ELEVATION_GRANTED = "tenant_elevation_granted"
    TENANT_ELEVATION_ENDED = "tenant_elevation_ended"
    PROVIDER_ENABLED = "provider_enabled"
    PROVIDER_DISABLED = "provider_disabled"
    PERSONA_TWEAK_UPDATED = "persona_tweak_updated"
    # Platform-tier authorization decisions. The PHI audit_log is tenant-scoped and
    # cannot hold a null-tenant row, so a SUPER_ADMIN's authz on a /platform route is
    # recorded here instead (ADR-0006). Tenant-route authz stays in audit_log.
    AUTHZ_ALLOW = "authz_allow"
    AUTHZ_DENY = "authz_deny"


def values_of(enum_cls: type[enum.StrEnum]) -> tuple[str, ...]:
    return tuple(member.value for member in enum_cls)


def check_in(
    column: str, enum_cls: type[enum.StrEnum], *, name: str | None = None
) -> CheckConstraint:
    """A `CHECK (column IN ('a','b',...))` built from a StrEnum catalog.

    `name` feeds the `ck_%(table)s_%(constraint_name)s` naming convention, so the
    final constraint is e.g. `ck_call_call_status_valid`.
    """
    quoted = ", ".join(f"'{value}'" for value in values_of(enum_cls))
    return CheckConstraint(f"{column} IN ({quoted})", name=name or f"{column}_valid")
