import { afterEach, describe, expect, it, vi } from "vitest"
import { copyText } from "./clipboard"

/** Minimal DOM stubs for the node test environment. */
function stubDom({ execCommandResult }: { execCommandResult: boolean }) {
  const textarea = {
    value: "",
    setAttribute: vi.fn(),
    focus: vi.fn(),
    select: vi.fn(),
    remove: vi.fn(),
    style: {} as Record<string, string>,
  }
  const execCommand = vi.fn().mockReturnValue(execCommandResult)
  vi.stubGlobal("document", {
    createElement: vi.fn().mockReturnValue(textarea),
    // querySelector returns null (no dialog present) — copyText falls back to body.
    querySelector: vi.fn().mockReturnValue(null),
    body: { appendChild: vi.fn() },
    execCommand,
  })
  return { textarea, execCommand }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("copyText", () => {
  it("uses navigator.clipboard in a secure context", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal("navigator", { clipboard: { writeText } })
    vi.stubGlobal("window", { isSecureContext: true })

    await expect(copyText("https://example.com/invite")).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith("https://example.com/invite")
  })

  it("falls back to execCommand when navigator.clipboard is unavailable (HTTP deployment)", async () => {
    // Non-secure context: navigator.clipboard is undefined.
    vi.stubGlobal("navigator", {})
    vi.stubGlobal("window", { isSecureContext: false })
    const { textarea, execCommand } = stubDom({ execCommandResult: true })

    await expect(copyText("https://example.com/invite")).resolves.toBe(true)
    expect(textarea.value).toBe("https://example.com/invite")
    expect(execCommand).toHaveBeenCalledWith("copy")
    expect(textarea.remove).toHaveBeenCalled()
  })

  it("falls back to execCommand when the clipboard API rejects", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"))
    vi.stubGlobal("navigator", { clipboard: { writeText } })
    vi.stubGlobal("window", { isSecureContext: true })
    const { execCommand } = stubDom({ execCommandResult: true })

    await expect(copyText("token")).resolves.toBe(true)
    expect(execCommand).toHaveBeenCalledWith("copy")
  })

  it("returns false when every copy mechanism fails", async () => {
    vi.stubGlobal("navigator", {})
    vi.stubGlobal("window", { isSecureContext: false })
    stubDom({ execCommandResult: false })

    await expect(copyText("anything")).resolves.toBe(false)
  })
})
