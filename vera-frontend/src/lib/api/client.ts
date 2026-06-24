// The single HTTP entry point to the control-plane API. It injects the bearer
// token, unwraps the backend's response envelope (`{ data, status, ... }`), and
// raises a typed `ApiError` whenever the call fails so callers branch on one
// error type instead of inspecting raw responses.

import { getToken } from "@/lib/auth/storage"

export const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api/v1"

// Registered by the store so a 401 can clear auth state without this module
// importing the store (avoids a circular dependency).
let authFailureHandler: (() => void) | null = null
export function registerAuthFailureHandler(handler: () => void): void {
  authFailureHandler = handler
}

/** Every backend response rides in this envelope (success or failure). */
type Envelope<T> = {
  data: T | null
  status: "SUCCESS" | "FAIL"
  message: string
  error_code: string | null
  description: string | null
}

/** Thrown on any non-success outcome: network failure, HTTP error, or a
 *  `status: "FAIL"` envelope. `errorCode` is the backend's stable code. */
export class ApiError extends Error {
  readonly httpStatus: number
  readonly errorCode: string | null

  constructor(httpStatus: number, errorCode: string | null, message: string) {
    super(message)
    this.name = "ApiError"
    this.httpStatus = httpStatus
    this.errorCode = errorCode
  }
}

type RequestOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE"
  body?: unknown
  /** Attach the stored bearer token. Defaults to true; false for login. */
  auth?: boolean
  /** Extra headers (e.g. Idempotency-Key). */
  headers?: Record<string, string>
}

export async function apiRequest<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, auth = true, headers: extraHeaders } = opts

  const headers: Record<string, string> = {
    ...extraHeaders,
    "Content-Type": "application/json",
  }
  if (auth) {
    const token = getToken()
    if (token) headers.Authorization = `Bearer ${token}`
  }

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

  let envelope: Envelope<T> | null = null
  try {
    envelope = (await res.json()) as Envelope<T>
  } catch {
    // Non-JSON body (e.g. a proxy/gateway error page) — handled below.
  }

  if (!res.ok || !envelope || envelope.status === "FAIL") {
    // 401 → session is gone/expired. Let the app clear auth state + redirect.
    // 403 is intentionally NOT treated here — callers handle access-denied.
    if (res.status === 401 && auth) authFailureHandler?.()
    throw new ApiError(
      res.status,
      envelope?.error_code ?? null,
      envelope?.message ?? `Request failed (${res.status}).`,
    )
  }

  return envelope.data as T
}
