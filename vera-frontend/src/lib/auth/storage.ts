// Where the session lives in the browser: held in memory and mirrored to
// sessionStorage so a page refresh doesn't force re-login, while still clearing
// when the tab closes. We persist the opaque session token plus the tenant slug
// (the URL handle every API call needs). The token carries no data — who it
// belongs to is fetched from /auth/me on demand. Session timeouts also come from
// /auth/me, so no session start time is stored client-side.

const TOKEN_KEY = "vera.session_token"
const TENANT_SLUG_KEY = "vera.tenant_slug"
// Which login plane an in-flight MFA challenge belongs to — a non-sensitive hint (no
// token) so a refresh mid-enrollment bounces back to the right login, not the tenant one.
const AUTH_PLANE_KEY = "vera.auth_plane"

let token: string | null = sessionStorage.getItem(TOKEN_KEY)
let tenantSlug: string | null = sessionStorage.getItem(TENANT_SLUG_KEY)
let authPlane: string | null = sessionStorage.getItem(AUTH_PLANE_KEY)

export function getToken(): string | null {
  return token
}

export function getTenantSlug(): string | null {
  return tenantSlug
}

export function getAuthPlane(): string | null {
  return authPlane
}

export function setAuthPlane(plane: "platform" | "tenant"): void {
  authPlane = plane
  sessionStorage.setItem(AUTH_PLANE_KEY, plane)
}

export function setSession(nextToken: string, nextTenantSlug: string): void {
  token = nextToken
  tenantSlug = nextTenantSlug
  sessionStorage.setItem(TOKEN_KEY, nextToken)
  sessionStorage.setItem(TENANT_SLUG_KEY, nextTenantSlug)
  clearAuthPlane()  // challenge complete — the pending-plane hint is moot
}

export function clearSession(): void {
  token = null
  tenantSlug = null
  sessionStorage.removeItem(TOKEN_KEY)
  sessionStorage.removeItem(TENANT_SLUG_KEY)
  clearAuthPlane()
}

function clearAuthPlane(): void {
  authPlane = null
  sessionStorage.removeItem(AUTH_PLANE_KEY)
}
