// The single HTTP entry point to the control-plane API. It injects the bearer
// token, unwraps the backend's response envelope (`{ data, status, ... }`), and
// raises a typed `ApiError` whenever the call fails so callers branch on one
// error type instead of inspecting raw responses.

import { getToken } from "@/lib/auth/storage"

export const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1"

// A unique token for headers like `Idempotency-Key`. `crypto.randomUUID()` only
// exists in a *secure context* (HTTPS, or http on localhost/127.0.0.1) — on a
// plain-HTTP origin such as `http://<ip>/` it is `undefined` and throws, killing
// the request before it leaves the browser. This degrades to a UUIDv4 built from
// `Math.random()`. That is NOT cryptographically strong, which is fine here: the
// value only needs to be unique-per-attempt for de-dup, never a secret.
export function randomId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID()
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16)
  })
}

// Registered by the store so a 401 can clear auth state without this module
// importing the store (avoids a circular dependency).
let authFailureHandler: (() => void) | null = null
export function registerAuthFailureHandler(handler: () => void): void {
  authFailureHandler = handler
}

// 401 → the session behind the request is gone/expired; let the app clear auth state and
// redirect. Only when the request carried the CURRENT token, though: a stale 401 from a
// superseded session must not force-logout a newer login (VR2-202).
// 403 is intentionally NOT treated here — callers handle access-denied.
function reportAuthFailure(status: number, auth: boolean, sentToken: string | null): void {
  if (status === 401 && auth && sentToken === getToken()) authFailureHandler?.()
}

/** Every backend response rides in this envelope (success or failure). */
type Envelope<T> = {
  data: T | null
  status: "SUCCESS" | "FAIL"
  message: string
  error_code: string | null
  description: string | null
}

// ApiError and its serialization helpers live in ./errors (dependency-free);
// re-exported here so existing `from "@/lib/api/client"` imports keep working.
export {
  ApiError,
  apiErrorFieldPaths,
  apiErrorHttpStatus,
  apiErrorMessage,
  serializeApiError,
} from "@/lib/api/errors"
import { ApiError } from "@/lib/api/errors"

type RequestOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
  body?: unknown
  /** Attach the stored bearer token. Defaults to true; false for login. */
  auth?: boolean
  /** Extra headers (e.g. Idempotency-Key). */
  headers?: Record<string, string>
  /** Cancel the in-flight request (e.g. on unmount) — nothing is sent past abort. */
  signal?: AbortSignal
}

/** apiRequest for binary downloads: returns the raw Blob. Failures still arrive
 *  as the JSON envelope, so parse it for the error message when present. */
export async function apiRequestBlob(path: string, opts: RequestOptions = {}): Promise<Blob> {
  const { method = "GET", body, auth = true, headers: extraHeaders } = opts
  const headers: Record<string, string> = { ...extraHeaders }
  if (body !== undefined) headers["Content-Type"] = "application/json"
  const sentToken = auth ? getToken() : null
  if (sentToken) headers.Authorization = `Bearer ${sentToken}`
  let res: Response
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "Could not reach the server. Is the API running?")
  }
  if (!res.ok) {
    let envelope: Envelope<unknown> | null = null
    try {
      envelope = (await res.json()) as Envelope<unknown>
    } catch {
      /* non-JSON error body */
    }
    reportAuthFailure(res.status, auth, sentToken)
    throw new ApiError(
      res.status,
      envelope?.error_code ?? null,
      envelope?.message ?? `Request failed (${res.status})`,
    )
  }
  return res.blob()
}

export async function apiRequest<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, auth = true, headers: extraHeaders, signal } = opts

  // A FormData body (multipart upload) must NOT get a JSON Content-Type or be
  // stringified — the browser sets its own boundary-bearing header when the
  // header is left unset.
  const isFormData = body instanceof FormData
  const headers: Record<string, string> = {
    ...extraHeaders,
    ...(isFormData ? {} : { "Content-Type": "application/json" }),
  }
  const sentToken = auth ? getToken() : null
  if (sentToken) headers.Authorization = `Bearer ${sentToken}`

  let res: Response
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : isFormData ? body : JSON.stringify(body),
      signal,
    })
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "Could not reach the server. Is the API running?")
  }

  let envelope: Envelope<T> | null = null
  try {
    envelope = (await res.json()) as Envelope<T>
  } catch {
    // Non-JSON body (e.g. a proxy/gateway error page) — handled below.
  }

  if (!res.ok || !envelope || envelope.status === "FAIL") {
    reportAuthFailure(res.status, auth, sentToken)
    throw new ApiError(
      res.status,
      envelope?.error_code ?? null,
      envelope?.message ?? `Request failed (${res.status}).`,
      envelope?.data,
    )
  }

  return envelope.data as T
}
