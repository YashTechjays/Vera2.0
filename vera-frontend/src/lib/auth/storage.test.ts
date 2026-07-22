import { afterEach, describe, expect, it, vi } from "vitest"

// storage.ts reads `sessionStorage` at module load time, so each test stubs the
// global BEFORE a fresh dynamic import (vi.resetModules forces re-evaluation).
describe("clearSession", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it("clears the notification read cursor along with the session (shared-tab shift change)", async () => {
    const backing = new Map<string, string>()
    vi.stubGlobal("sessionStorage", {
      getItem: (k: string) => backing.get(k) ?? null,
      setItem: (k: string, v: string) => void backing.set(k, v),
      removeItem: (k: string) => void backing.delete(k),
    })
    vi.resetModules()
    const { setSession, clearSession, getToken } = await import("@/lib/auth/storage")
    const { saveReadCursor, loadReadCursor } = await import("@/lib/notifications/store")

    setSession("tok", "tenant-a")
    saveReadCursor("100-0")
    expect(loadReadCursor()).toBe("100-0")

    clearSession()

    // Without this, a second user logging in on the same tab right after would
    // inherit the first user's cursor and silently miss unread alerts.
    expect(loadReadCursor()).toBeNull()
    expect(getToken()).toBeNull()
  })
})
