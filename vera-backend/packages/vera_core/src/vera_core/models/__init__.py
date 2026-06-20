"""Every model is imported here so a single `import vera_core.models` registers
the whole schema on `Base.metadata` (used by Alembic and `create_all`)."""

from .app_user import AppUser
from .audit_log import AuditLog
from .auth import (
    AuthAuditLog,
    PlatformLoginProvider,
    SsoProvider,
    TenantElevation,
    UserIdentity,
)
from .authoring import FormSchema, Prompt, PromptVersion, SchemaVersion
from .call import Call, CallEvent, CallLineage
from .field_answer import CallFormSnapshot, DisputeAction, FieldAnswer, FieldEvaluation
from .insurance import InsuranceProvider, IvrPlaybook
from .integrations import ApiKey, Integration, IntegrationType
from .oversight import (
    CallProviderUsage,
    EvalRun,
    ExportArtifact,
    HumanRating,
    InterventionEvent,
)
from .patient_form import PatientForm
from .rbac import Permission, Role, RolePermission, UserRole
from .tenant import Tenant
from .transcript import Recording, Transcript

__all__ = [
    "ApiKey",
    "AppUser",
    "AuditLog",
    "AuthAuditLog",
    "Call",
    "CallEvent",
    "CallFormSnapshot",
    "CallLineage",
    "CallProviderUsage",
    "DisputeAction",
    "EvalRun",
    "ExportArtifact",
    "FieldAnswer",
    "FieldEvaluation",
    "FormSchema",
    "HumanRating",
    "InsuranceProvider",
    "Integration",
    "IntegrationType",
    "InterventionEvent",
    "IvrPlaybook",
    "PatientForm",
    "Permission",
    "PlatformLoginProvider",
    "Prompt",
    "PromptVersion",
    "Recording",
    "Role",
    "RolePermission",
    "SchemaVersion",
    "SsoProvider",
    "Tenant",
    "TenantElevation",
    "Transcript",
    "UserIdentity",
    "UserRole",
]
