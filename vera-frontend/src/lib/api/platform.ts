// Platform-operator (super admin) endpoints. An elevation is a time-boxed grant
// that lets a platform operator act inside one tenant; tenant-scoped routes only
// work while an active grant exists. All require a platform session + the
// platform:elevations:* permissions (held by SUPER_ADMIN).

import { apiRequest, randomId } from "@/lib/api/client"

/** Max break-glass window the backend accepts (8 hours). */
export const MAX_ELEVATION_MINUTES = 480
/** Max reason length the backend accepts. */
export const MAX_ELEVATION_REASON = 2000

export type Elevation = {
  id: string
  target_tenant_id: string
  reason: string
  granted_at: string
  expires_at: string
  ended_at: string | null
}

export type CreateElevationInput = {
  target_tenant_id: string
  reason: string
  duration_minutes: number
}

/** A deactivated tenant's slug stops resolving at login, so its users cannot sign in. */
export type TenantStatus = "active" | "deactivated"

/** What `listTenants` may ask for. "all" is what the management table needs; the
 *  elevation picker relies on the active-only default and must never pass this. */
export type TenantListStatus = TenantStatus | "all"

export type TenantSummary = {
  id: string
  name: string
  slug: string
  status: TenantStatus
  region: string | null
  created_at: string
  /** AI form-filling (observer) master switch for this tenant. `null` means the API did
   *  not disclose it — the caller lacks `platform:tenants:manage` — not that it is off. */
  observer_enabled: boolean | null
  /** Auto-retry master switch for this tenant. `null` means the API did not disclose it —
   *  the caller lacks `platform:tenants:manage` — not that it is off. */
  auto_retry_enabled: boolean | null
  /** Fill-rate threshold (0–1) below which a bot-ended call is auto-retried. `null` means
   *  the API did not disclose it — the caller lacks `platform:tenants:manage`. */
  retry_fill_threshold: number | null
}

/** Full settings for one tenant. Nothing is withheld here — the detail route requires
 *  `platform:tenants:manage`, unlike the list which also serves the elevation picker. */
export type TenantDetail = {
  id: string
  name: string
  slug: string
  status: TenantStatus
  region: string | null
  created_at: string
  observer_enabled: boolean
  auto_retry_enabled: boolean
  retry_fill_threshold: number
  max_agents_per_va: number
  max_concurrent_calls: number
  max_retries: number
  queue_expiry_hours: number
  recording_retention_days: number | null
}

/** `slug` is immutable after creation — it is part of the tenant's login URL. */
export type CreateTenantInput = {
  name: string
  slug: string
  region?: string
}

/** Every editable setting. `slug` and `status` are deliberately absent: the slug is
 *  immutable, and status changes go through deactivate/reactivate. */
export type UpdateTenantInput = Partial<{
  name: string
  region: string | null
  observer_enabled: boolean
  auto_retry_enabled: boolean
  retry_fill_threshold: number
  max_agents_per_va: number
  max_concurrent_calls: number
  max_retries: number
  queue_expiry_hours: number
  recording_retention_days: number | null
}>

/** Tenants for the elevation picker, the Platform Settings screen, and the Tenants
 *  admin table. Defaults to ACTIVE ONLY — pass `status` to widen, which requires
 *  `platform:tenants:manage` (the picker must keep the default so a switched-off
 *  client can never be elevated into). */
export function listTenants(params?: { status?: TenantListStatus }) {
  const query = params?.status ? `?status=${params.status}` : ""
  return apiRequest<TenantSummary[]>(`/platform/tenants${query}`)
}

function tenantPath(tenantId: string, suffix = ""): string {
  return `/platform/tenants/${encodeURIComponent(tenantId)}${suffix}`
}

/** Create a client organisation. Does NOT invite anyone — use `inviteTenantUser` after.
 *  409 if the slug is taken. Requires `platform:tenants:manage`. */
export function createTenant(input: CreateTenantInput) {
  return apiRequest<TenantDetail>("/platform/tenants", {
    method: "POST",
    body: input,
    headers: { "Idempotency-Key": randomId() },
  })
}

export function getTenant(tenantId: string) {
  return apiRequest<TenantDetail>(tenantPath(tenantId))
}

/** Omitted fields stay unchanged. Requires `platform:tenants:manage`. Sends no
 *  Idempotency-Key: every field is an absolute value, so the PATCH is naturally
 *  idempotent and the backend route deliberately skips the gate. */
export function updateTenant(tenantId: string, patch: UpdateTenantInput) {
  return apiRequest<TenantDetail>(tenantPath(tenantId), {
    method: "PATCH",
    body: patch,
  })
}

/** Blocks NEW logins for this tenant's users; sessions already open run to expiry. */
export function deactivateTenant(tenantId: string) {
  return apiRequest<TenantDetail>(tenantPath(tenantId, "/deactivate"), {
    method: "POST",
    headers: { "Idempotency-Key": randomId() },
  })
}

export function reactivateTenant(tenantId: string) {
  return apiRequest<TenantDetail>(tenantPath(tenantId, "/reactivate"), {
    method: "POST",
    headers: { "Idempotency-Key": randomId() },
  })
}

export type TenantUser = {
  id: string
  email: string
  name: string
  status: string
  roles: string[]
}

export type InviteTenantUserInput = {
  email: string
  name: string
  roleIds: string[]
  sendEmail: boolean
}

export type InviteTenantUserResult = {
  user_id: string
  email: string
  invite_url: string
  email_sent: boolean
}

export type TenantRole = {
  id: string
  name: string
  is_system: boolean
}

/** Roles assignable inside one tenant: the GLOBAL system roles only, minus the
 *  platform-tier ones. A tenant's own custom roles are unreachable from the platform
 *  plane (their RLS needs a tenant context) — the tenant's Users screen assigns those. */
export function listTenantRoles(tenantId: string) {
  return apiRequest<TenantRole[]>(tenantPath(tenantId, "/roles"))
}

export function listTenantUsers(tenantId: string) {
  return apiRequest<TenantUser[]>(tenantPath(tenantId, "/users"))
}

/** Invite a user INTO a tenant from the platform plane — no elevation grant needed.
 *  Requires `platform:tenants:manage`. */
export function inviteTenantUser(tenantId: string, input: InviteTenantUserInput) {
  return apiRequest<InviteTenantUserResult>(tenantPath(tenantId, "/users/invitations"), {
    method: "POST",
    body: {
      email: input.email,
      name: input.name,
      role_ids: input.roleIds,
      send_email: input.sendEmail,
    },
    headers: { "Idempotency-Key": randomId() },
  })
}

export type TenantObserver = {
  tenant_id: string
  observer_enabled: boolean
}

/** Toggle a tenant's AI form-filling (observer) feature. Requires
 *  `platform:tenants:manage`. */
export function setTenantObserverEnabled(tenantId: string, enabled: boolean) {
  return apiRequest<TenantObserver>(tenantPath(tenantId, "/observer"), {
    method: "POST",
    body: { enabled },
    headers: { "Idempotency-Key": randomId() },
  })
}

export type TenantRetryConfig = {
  tenant_id: string
  auto_retry_enabled: boolean
  retry_fill_threshold: number
}

/** Set a tenant's auto-retry flag and/or fill threshold (0–1). Requires
 *  `platform:tenants:manage`. Omitted fields stay unchanged. */
export function setTenantRetryConfig(
  tenantId: string,
  patch: { auto_retry_enabled?: boolean; retry_fill_threshold?: number },
) {
  return apiRequest<TenantRetryConfig>(tenantPath(tenantId, "/retry-config"), {
    method: "POST",
    body: patch,
    headers: { "Idempotency-Key": randomId() },
  })
}

/** All active (un-ended, un-expired) grants. */
export function listElevations() {
  return apiRequest<Elevation[]>("/platform/elevations")
}

export function createElevation(input: CreateElevationInput) {
  return apiRequest<Elevation>("/platform/elevations", { method: "POST", body: input })
}

export function endElevation(id: string) {
  return apiRequest<null>(`/platform/elevations/${encodeURIComponent(id)}/end`, {
    method: "POST",
  })
}

export type Operator = {
  id: string
  email: string
  name: string
  /** "invited" | "active" | "deactivated" */
  status: string
  last_login_at: string | null
}

export type InviteOperatorInput = {
  email: string
  name: string
  sendEmail: boolean
}

export type InviteOperatorResult = {
  user_id: string
  email: string
  invite_url: string
  email_sent: boolean
}

/** List all platform operators. Requires `platform:users:read`. */
export function listOperators() {
  return apiRequest<Operator[]>("/platform/users")
}

/** Invite a new platform operator (always granted SUPER_ADMIN). Requires
 *  `platform:users:invite`. */
export function inviteOperator(input: InviteOperatorInput) {
  return apiRequest<InviteOperatorResult>("/platform/users/invitations", {
    method: "POST",
    body: { email: input.email, name: input.name, send_email: input.sendEmail },
    headers: { "Idempotency-Key": randomId() },
  })
}

/** Deactivate a platform operator. Requires `platform:users:invite`. Blocked
 *  (409) if this would leave zero active operators. */
export function deactivateOperator(id: string) {
  return apiRequest<null>(`/platform/users/${encodeURIComponent(id)}/deactivate`, {
    method: "POST",
  })
}

/** Reissue a fresh invite link for an operator stuck in status="invited".
 *  Requires `platform:users:invite`. */
export function resendOperatorInvitation(id: string) {
  return apiRequest<InviteOperatorResult>(
    `/platform/users/${encodeURIComponent(id)}/resend-invitation`,
    { method: "POST", headers: { "Idempotency-Key": randomId() } },
  )
}
