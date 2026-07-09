// Typed API error plus helpers that survive Redux's error serialization.
// Kept dependency-free so store code and node-environment tests can import it
// without dragging in browser-only modules.

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

/** What an ApiError survives as after `serializeApiError`. */
export type SerializedApiError = {
  name: string
  message: string
  httpStatus?: number
  errorCode?: string | null
}

/** `createAsyncThunk` strips thrown errors down to `{name, message}`, losing the
 * ApiError class and its httpStatus — so `instanceof ApiError` is always false
 * after `unwrap()`. Pass this as the thunk's `serializeError` option to keep the
 * API details; read them back with `apiErrorMessage` / `apiErrorHttpStatus`. */
export function serializeApiError(err: unknown): SerializedApiError {
  if (err instanceof ApiError) {
    return {
      name: err.name,
      message: err.message,
      httpStatus: err.httpStatus,
      errorCode: err.errorCode,
    }
  }
  if (err instanceof Error) return { name: err.name, message: err.message }
  return { name: "Error", message: String(err) }
}

function asApiError(err: unknown): ApiError | SerializedApiError | null {
  if (err instanceof ApiError) return err
  if (
    typeof err === "object" &&
    err !== null &&
    (err as SerializedApiError).name === "ApiError" &&
    typeof (err as SerializedApiError).message === "string"
  ) {
    return err as SerializedApiError
  }
  return null
}

/** The backend-provided message, or `fallback` when the failure wasn't an API
 * error (network TypeError, programming bug, …) whose message would be noise. */
export function apiErrorMessage(err: unknown, fallback: string): string {
  return asApiError(err)?.message ?? fallback
}

/** HTTP status of an ApiError — live or serialized — or null. */
export function apiErrorHttpStatus(err: unknown): number | null {
  return asApiError(err)?.httpStatus ?? null
}
