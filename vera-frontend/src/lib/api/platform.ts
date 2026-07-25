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

export type TenantSummary = {
  id: string
  name: string
  slug: string
  /** AI form-filling (observer) master switch for this tenant. */
  observer_enabled: boolean
}

/** Active tenants the operator can elevate into (the tenant picker source) and manage
 *  on the Platform Settings screen. */
export function listTenants() {
  return apiRequest<TenantSummary[]>("/platform/tenants")
}

export type TenantObserver = {
  tenant_id: string
  observer_enabled: boolean
}

/** Toggle a tenant's AI form-filling (observer) feature. Requires
 *  `platform:tenants:manage`. */
export function setTenantObserverEnabled(tenantId: string, enabled: boolean) {
  return apiRequest<TenantObserver>(
    `/platform/tenants/${encodeURIComponent(tenantId)}/observer`,
    {
      method: "POST",
      body: { enabled },
      headers: { "Idempotency-Key": randomId() },
    },
  )
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
