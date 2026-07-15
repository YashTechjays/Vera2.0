import { describe, expect, it } from "vitest"
import {
  ApiError,
  apiErrorHttpStatus,
  apiErrorMessage,
  serializeApiError,
} from "./errors"

describe("serializeApiError + helpers", () => {
  it("preserves the API message and status across serialization (thunk boundary)", () => {
    const serialized = serializeApiError(new ApiError(401, "UNAUTHORIZED", "invalid credentials"))
    expect(apiErrorMessage(serialized, "fallback")).toBe("invalid credentials")
    expect(apiErrorHttpStatus(serialized)).toBe(401)
  })

  it("reads a live ApiError instance directly", () => {
    const err = new ApiError(422, "VALIDATION_ERROR", "code expired")
    expect(apiErrorMessage(err, "fallback")).toBe("code expired")
    expect(apiErrorHttpStatus(err)).toBe(422)
  })

  it("falls back for non-API errors", () => {
    const serialized = serializeApiError(new TypeError("x is not a function"))
    expect(apiErrorMessage(serialized, "Something went wrong.")).toBe("Something went wrong.")
    expect(apiErrorHttpStatus(serialized)).toBeNull()
    expect(apiErrorMessage(undefined, "Something went wrong.")).toBe("Something went wrong.")
  })
})
