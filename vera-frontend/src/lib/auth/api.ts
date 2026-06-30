// Thin typed wrappers over the control-plane auth endpoints, matching the backend
// contract exactly. Tenant-scoped routes are keyed by the human-readable slug.
// Token-scoped self endpoints (/auth/me, /auth/logout, /auth/session/keepalive,
// /auth/mfa/enroll, /auth/mfa/activate) carry NO slug.

import { apiRequest, randomId } from "@/lib/api/client"

export type LoginResult = {
  mfa: "none" | "verify" | "enroll"
  session_token: string | null
  mfa_token: string | null
  provisioning_uri: string | null
}
export type SessionResult = { session_token: string }
export type EnrollActivateResult = { session_token: string; recovery_codes: string[] }
export type AcceptInviteResult = {
  mfa_required: boolean
  provisioning_uri: string | null
  mfa_token: string | null
}
export type RecoveryCodesResult = { recovery_codes: string[] }
export type KeepaliveResult = { expires_in_seconds: number }

export type MeResponse = {
  user_id: string
  email: string
  name: string
  account_type: string
  tenant_id: string | null
  tenant_slug: string | null
  roles: string[]
  permissions: string[]
  /** A platform operator's current elevation grant; null for tenant users and
   *  for un-elevated operators. Drives elevation-aware UI (e.g. the sidebar). */
  active_elevation: { target_tenant_id: string; expires_at: string } | null
}

export type InviteUserResult = {
  user_id: string
  email: string
  invite_url: string
  email_sent: boolean
}

const tenantAuth = (slug: string) => `/tenants/${encodeURIComponent(slug)}/auth`

export function login(slug: string, email: string, password: string) {
  return apiRequest<LoginResult>(`${tenantAuth(slug)}/login`, {
    method: "POST",
    body: { email, password },
    auth: false,
  })
}

export function verifyMfa(slug: string, mfaToken: string, code: string) {
  return apiRequest<SessionResult>(`${tenantAuth(slug)}/mfa/verify`, {
    method: "POST",
    body: { mfa_token: mfaToken, code },
    auth: false,
  })
}

// --- Platform-operator (super admin) auth: NO tenant slug. Login ALWAYS returns
// an MFA challenge (mandatory); verify mints a platform session (tenant_id stays
// NULL — the operator then elevates into a tenant). ---
export function platformLogin(email: string, password: string) {
  return apiRequest<LoginResult>(`/platform/auth/login`, {
    method: "POST",
    body: { email, password },
    auth: false,
  })
}

export function platformVerifyMfa(mfaToken: string, code: string) {
  return apiRequest<SessionResult>(`/platform/auth/mfa/verify`, {
    method: "POST",
    body: { mfa_token: mfaToken, code },
    auth: false,
  })
}

export function enrollActivate(slug: string, mfaToken: string, code: string) {
  return apiRequest<EnrollActivateResult>(`${tenantAuth(slug)}/mfa/enroll-activate`, {
    method: "POST",
    body: { mfa_token: mfaToken, code },
    auth: false,
  })
}

export function acceptInvite(slug: string, token: string, password: string) {
  return apiRequest<AcceptInviteResult>(`${tenantAuth(slug)}/invitations/accept`, {
    method: "POST",
    body: { token, password },
    auth: false,
  })
}

export function activateInviteMfa(slug: string, mfaToken: string, code: string) {
  return apiRequest<RecoveryCodesResult>(`${tenantAuth(slug)}/invitations/activate-mfa`, {
    method: "POST",
    body: { mfa_token: mfaToken, code },
    auth: false,
  })
}

export function getMe() {
  return apiRequest<MeResponse>(`/auth/me`)
}

export function logout() {
  return apiRequest<null>(`/auth/logout`, { method: "POST" })
}

export function keepalive() {
  return apiRequest<KeepaliveResult>(`/auth/session/keepalive`, { method: "POST" })
}

export function inviteUser(input: {
  email: string
  name: string
  roleIds: string[]
  sendEmail: boolean
}) {
  return apiRequest<InviteUserResult>(`/users/invitations`, {
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

export type EnrollResult = { provisioning_uri: string }

/** Authenticated self-service MFA enrollment: mint a fresh TOTP seed and return
 *  its provisioning URI. Token-scoped (no slug). */
export function enrollMfa() {
  return apiRequest<EnrollResult>(`/auth/mfa/enroll`, { method: "POST" })
}

/** Confirm the live TOTP code to activate MFA; returns one-time recovery codes. */
export function activateMfa(code: string) {
  return apiRequest<RecoveryCodesResult>(`/auth/mfa/activate`, {
    method: "POST",
    body: { code },
  })
}

export type UserSummary = {
  id: string
  email: string
  name: string
  /** "invited" | "active" | "deactivated" */
  status: string
  last_login_at: string | null
}

/** List all users in the caller's tenant (RLS-scoped). Requires `users:read`. */
export function listUsers() {
  return apiRequest<UserSummary[]>(`/users`)
}

/** Deactivate a tenant user. Requires `users:manage`. */
export function deactivateUser(userId: string) {
  return apiRequest<null>(`/users/${encodeURIComponent(userId)}/deactivate`, {
    method: "POST",
  })
}
