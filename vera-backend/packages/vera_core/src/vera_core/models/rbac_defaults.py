"""Permission catalog and the seeded global role templates.

Role identifiers are the ADR-style role names (the role's `name` column IS the
identifier). Per spec §4.1.1 / §5.1, the seeded roles are **global system/template
roles** (`role.tenant_id IS NULL`) shared across all tenants: a tenant assigns them
to its users via `user_role` without per-tenant copies, and defines its own custom
roles (`tenant_id` set) on top. `SUPER_ADMIN` is global too, but it carries
platform-tier permissions, so it is never tenant-assignable — the role-assignment
endpoint rejects any role holding a `platform:*` permission (only a platform
operator grants `SUPER_ADMIN`). `phi:detokenize` is its own permission, granted to
TENANT_ADMIN/SUPERVISOR and implied by nothing else."""

from typing import Final

DEFAULT_PERMISSIONS: Final[dict[str, str]] = {
    "calls:read": "View calls and their status/results",
    "calls:write": "Create and manage verification calls",
    "calls:publish": "Publish a call so other VAs in the tenant can view and intervene",
    "recordings:read": "Play back call recordings (every playback is audited)",
    "recordings:manage": "Manage the tenant's recording retention policy",
    "voice_lab:sandbox": "Use the Voice Lab sandbox to start and monitor test voice sessions",
    "forms:read": "View form templates and filled forms",
    "forms:write": "Create and edit form templates",
    "users:read": "View users in the tenant",
    "users:manage": "Invite, deactivate, and manage users",
    "roles:manage": "Manage roles and role assignments",
    "tenant:auth:configure": "Enable or disable the tenant's login providers",
    "tenant:config:manage": "View and edit tenant runtime config (persona, knobs)",
    "apikeys:manage": "Issue and revoke inbound API keys",
    "integrations:manage": "Configure outbound integration credentials (e.g. Twilio)",
    "audit:read": "Read the compliance audit log",
    "phi:detokenize": "Reveal raw PHI behind tokens (every use is audited)",
}

# Platform-tier permissions (ADR-0006). Granted ONLY to the global SUPER_ADMIN role,
# never to any tenant role — a TENANT_ADMIN must not be able to grant cross-tenant
# break-glass access. Kept separate from DEFAULT_PERMISSIONS for exactly that reason.
PLATFORM_PERMISSIONS: Final[dict[str, str]] = {
    "platform:elevations:create": "Open a scoped, time-boxed tenant elevation (break-glass)",
    "platform:elevations:end": "End an active tenant elevation early",
    "platform:elevations:read": "List active tenant elevations (platform oversight)",
    "platform:prompts:read": "View the prompt authoring catalog and its versions",
    "platform:prompts:write": "Create and publish prompt versions",
    "platform:ivr_playbooks:read": "View insurance providers and their IVR playbooks",
    "platform:ivr_playbooks:write": "Create and manage insurance providers and IVR playbooks",
}

# The full catalog seeded into `permission` (tenant + platform).
ALL_PERMISSIONS: Final[dict[str, str]] = {**DEFAULT_PERMISSIONS, **PLATFORM_PERMISSIONS}

# Global system/template roles (tenant_id IS NULL), seeded once and shared across
# tenants. SUPER_ADMIN holds every permission (tenant + platform) and is NOT
# tenant-assignable (it carries platform perms); TENANT_ADMIN and SUPERVISOR hold
# only tenant permissions. Privilege comes only from an RBAC grant, never from
# account_type. Tenants add custom roles separately.
SYSTEM_ROLES: Final[dict[str, frozenset[str]]] = {
    "SUPER_ADMIN": frozenset(ALL_PERMISSIONS),
    "TENANT_ADMIN": frozenset(DEFAULT_PERMISSIONS),
    "SUPERVISOR": frozenset(
        {
            "calls:read",
            "calls:write",
            "calls:publish",
            "recordings:read",
            "voice_lab:sandbox",
            "forms:read",
            "forms:write",
            "users:read",
            "audit:read",
            "phi:detokenize",
        }
    ),
    "VIRTUAL_ASSISTANT": frozenset({"voice_lab:sandbox"}),
}
