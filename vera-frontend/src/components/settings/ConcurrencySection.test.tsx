import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ConcurrencySection } from "@/components/settings/ConcurrencySection"
import { getConcurrencyConfig, patchConcurrencyConfig } from "@/lib/api/tenantConfig"

vi.mock("@/lib/api/tenantConfig", () => ({
  getConcurrencyConfig: vi.fn(),
  patchConcurrencyConfig: vi.fn(),
}))

const mockedGet = vi.mocked(getConcurrencyConfig)
const mockedPatch = vi.mocked(patchConcurrencyConfig)

describe("ConcurrencySection", () => {
  beforeEach(() => {
    mockedGet.mockReset()
    mockedPatch.mockReset()
    mockedGet.mockResolvedValue({ max_agents_per_va: 3, max_concurrent_calls: 25 })
  })

  it("loads and renders both knobs", async () => {
    render(<ConcurrencySection />)
    await userEvent.click(screen.getByText("Agent capacity")) // expand the card

    await waitFor(() => {
      expect(screen.getByLabelText(/agents per va/i)).toHaveValue(3)
      expect(screen.getByLabelText(/tenant call ceiling/i)).toHaveValue(25)
    })
  })

  it("saves changed knobs via PATCH", async () => {
    mockedPatch.mockResolvedValue({ max_agents_per_va: 5, max_concurrent_calls: 25 })
    render(<ConcurrencySection />)
    await userEvent.click(screen.getByText("Agent capacity"))
    await waitFor(() => expect(screen.getByLabelText(/agents per va/i)).toHaveValue(3))

    const perVa = screen.getByLabelText(/agents per va/i)
    await userEvent.clear(perVa)
    await userEvent.type(perVa, "5")
    await userEvent.click(screen.getByRole("button", { name: /save/i }))

    // Only the edited knob is sent — unchanged fields from a possibly stale local
    // copy must not ride along and clobber another admin's concurrent change.
    await waitFor(() => expect(mockedPatch).toHaveBeenCalledWith({ max_agents_per_va: 5 }))
  })

  it("does not PATCH when nothing changed", async () => {
    render(<ConcurrencySection />)
    await userEvent.click(screen.getByText("Agent capacity"))
    await waitFor(() => expect(screen.getByLabelText(/agents per va/i)).toHaveValue(3))

    await userEvent.click(screen.getByRole("button", { name: /save/i }))

    expect(mockedPatch).not.toHaveBeenCalled()
  })

  it("surfaces the API error message on a failed save", async () => {
    const { ApiError } = await import("@/lib/api/client")
    mockedPatch.mockRejectedValue(
      new ApiError(422, "VALIDATION_ERROR", "Validation failed."),
    )
    render(<ConcurrencySection />)
    await userEvent.click(screen.getByText("Agent capacity"))
    await waitFor(() => expect(screen.getByLabelText(/agents per va/i)).toHaveValue(3))

    const perVa = screen.getByLabelText(/agents per va/i)
    await userEvent.clear(perVa)
    await userEvent.type(perVa, "5")
    await userEvent.click(screen.getByRole("button", { name: /save/i }))

    await waitFor(() => expect(screen.getByText("Validation failed.")).toBeInTheDocument())
  })
})
