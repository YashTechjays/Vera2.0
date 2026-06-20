// Where the session lives in the browser: held in memory and mirrored to
// sessionStorage so a page refresh doesn't force re-login, while still clearing
// when the tab closes. We persist the opaque session token plus the tenant slug
// (the URL handle every API call needs). The token carries no data — who it
// belongs to is fetched from /auth/me on demand.

const TOKEN_KEY = "vera.session_token"
const TENANT_SLUG_KEY = "vera.tenant_slug"
const SESSION_START_KEY = "vera.session_started_at"

let token: string | null = sessionStorage.getItem(TOKEN_KEY)
let tenantSlug: string | null = sessionStorage.getItem(TENANT_SLUG_KEY)

export function getToken(): string | null {
  return token
}

export function getTenantSlug(): string | null {
  return tenantSlug
}

export function getSessionStart(): number | null {
  const raw = sessionStorage.getItem(SESSION_START_KEY)
  return raw ? Number(raw) : null
}

export function setSession(nextToken: string, nextTenantSlug: string): void {
  token = nextToken
  tenantSlug = nextTenantSlug
  sessionStorage.setItem(TOKEN_KEY, nextToken)
  sessionStorage.setItem(TENANT_SLUG_KEY, nextTenantSlug)
  sessionStorage.setItem(SESSION_START_KEY, String(Date.now()))
}

export function clearSession(): void {
  token = null
  tenantSlug = null
  sessionStorage.removeItem(TOKEN_KEY)
  sessionStorage.removeItem(TENANT_SLUG_KEY)
  sessionStorage.removeItem(SESSION_START_KEY)
}
