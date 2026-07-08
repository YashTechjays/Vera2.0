import { ApiError } from "@/lib/api/client"

/** ApiError as plain data — class instances don't survive RTK's rejection serialization. */
export type ApiErrorPayload = {
  kind: "api-error"
  message: string
  httpStatus: number
  errorCode: string | null
}

export function toApiErrorPayload(err: unknown): ApiErrorPayload | null {
  if (err instanceof ApiError) {
    return {
      kind: "api-error",
      message: err.message,
      httpStatus: err.httpStatus,
      errorCode: err.errorCode,
    }
  }
  return null
}

function isApiErrorPayload(err: unknown): err is ApiErrorPayload {
  return typeof err === "object" && err !== null && (err as ApiErrorPayload).kind === "api-error"
}

/** Backend message from an ApiError or payload; the fallback for anything else. */
export function apiErrorMessage(err: unknown, fallback: string): string {
  if (err instanceof ApiError || isApiErrorPayload(err)) return err.message
  return fallback
}

export function apiErrorStatus(err: unknown): number | null {
  if (err instanceof ApiError || isApiErrorPayload(err)) return err.httpStatus
  return null
}
