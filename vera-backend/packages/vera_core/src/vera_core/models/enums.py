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
    EXPIRED = "expired"


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
    # User-requested end (End Call in Live Monitoring); never auto-retried.
    CANCELED = "canceled"


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


class RecordingStatus(enum.StrEnum):
    """recording lifecycle. PENDING at egress start; AVAILABLE once the object is
    sha256-verified; FAILED (egress start or run failed); DISCARDED (no-answer/busy
    call — object deleted at verify time); DELETED (retention-sweep tombstone)."""

    PENDING = "pending"
    AVAILABLE = "available"
    FAILED = "failed"
    DISCARDED = "discarded"
    DELETED = "deleted"


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


class CallHealthFlag(enum.StrEnum):
    """Call-health observer verdict vocabulary: the InterventionCategory values
    (kept in sync so intervention reports and observer flags speak one language)
    plus `none` (healthy) and `supervisor_requested` (the rep/IVR asked for a
    human). Stored on call.health_flag and in call_event HEALTH rows."""

    NONE = "none"
    SUPERVISOR_REQUESTED = "supervisor_requested"
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


class ReviewReason(enum.StrEnum):
    """Why the post-call pipeline routed a form to EXCEPTION_REVIEW. NULL on the
    form outside review and for manual transitions (a pipeline artifact)."""

    # Every required field is satisfied — nothing is wrong, the form just needs a
    # human to sign it off. The pipeline never auto-COMPLETEs; COMPLETED is a
    # human-only transition out of review (once any disputes are resolved).
    READY_FOR_REVIEW = "ready_for_review"
    TOKEN_VALUE = "token_value"
    RETRIES_EXHAUSTED = "retries_exhausted"
    LLM_ERROR = "llm_error"
    NO_TRANSCRIPT = "no_transcript"
    # Required fields are unsatisfied but none are askable — a retry call could
    # not fix them, so the form needs human review.
    UNSATISFIED_UNASKABLE = "unsatisfied_unaskable"
    # The form's pinned schema document failed to parse (e.g. legacy v1) — the
    # eval cannot run against it.
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    # A supervisor ended the call by hand — it is never auto-redialed even with
    # unsatisfied retryable fields; a human takes it from here.
    USER_ENDED = "user_ended"
    # Required fields are unsatisfied and retryable, but the deployment-wide
    # form_auto_retry_enabled flag is off — the eval never auto-redials, so
    # the form parks for a human instead of re-queueing.
    AUTO_RETRY_DISABLED = "auto_retry_disabled"
    # The automated post-call eval did not run for this form — either the eval
    # consumer isn't configured (no Vertex/Gemini), so the close path resolved
    # the form synchronously, or the pipeline sweeper reclaimed a form stranded
    # in AI_PROCESSING. A human reviews it without AI-extracted values.
    NOT_EVALUATED = "not_evaluated"


class VersionStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class InsuranceType(enum.StrEnum):
    """The insurance/form family a `form_schema` belongs to. Grows to carrier-level
    types (aetna/cigna/uhc) with a one-line addition plus a CHECK-update migration."""

    INFERTILITY_TREATMENT = "infertility_treatment"
    DISEASE_ONLY = "disease_only"


class ProviderStatus(enum.StrEnum):
    """insurance_provider lifecycle. Only ACTIVE providers are offered in the call-start
    picker and may steer a live IVR call; a free-text status silently dropped a provider
    from every `status == 'active'` lookup, hence the single catalog."""

    ACTIVE = "active"
    INACTIVE = "inactive"


class PlaybookStatus(enum.StrEnum):
    """ivr_playbook lifecycle. At most one ACTIVE playbook per provider drives runtime
    selection; the partial unique index and demote-then-promote both key on ACTIVE."""

    ACTIVE = "active"
    INACTIVE = "inactive"


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
    ROLE_UPDATED = "role_updated"
    ROLE_DELETED = "role_deleted"
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
    # Token-scoped self-logout (/auth/logout). Tenant users write a tenant-scoped
    # row; platform operators (tenant_id IS NULL) go through log_auth_event.
    LOGOUT = "logout"
    # Tenant-level recording retention policy updated (old/new day counts, no PHI).
    RETENTION_POLICY_UPDATED = "retention_policy_updated"
    # Tenant-tier invite resend (fixes a gap: neither the invite link nor the MFA
    # bridge token had any recovery path before this feature).
    INVITE_RESENT = "invite_resent"
    # Platform-operator lifecycle — kept distinct from the tenant USER_INVITED /
    # INVITE_ACCEPTED / USER_DEACTIVATED events so privilege-granting activity is
    # separately auditable.
    PLATFORM_USER_INVITED = "platform_user_invited"
    PLATFORM_INVITE_ACCEPTED = "platform_invite_accepted"
    PLATFORM_USER_ACTIVATED = "platform_user_activated"
    PLATFORM_USER_DEACTIVATED = "platform_user_deactivated"
    PLATFORM_INVITE_RESENT = "platform_invite_resent"


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
