import { render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { LiveAnalyticsPanel } from "@/components/analytics/LiveAnalyticsPanel"
import { getLivePanel } from "@/lib/api/analytics"

vi.mock("@/lib/api/analytics", () => ({ getLivePanel: vi.fn() }))

const mocked = vi.mocked(getLivePanel)

describe("LiveAnalyticsPanel", () => {
  // Concise-body arrow would return mockReset()'s return value (the mock itself),
  // which Vitest treats as an implicit afterEach teardown and re-invokes post-test.
  beforeEach(() => {
    mocked.mockReset()
  })

  it("renders one row per provider plus totals", async () => {
    mocked.mockResolvedValue({
      rows: [
        { provider_id: "a", provider_name: "Aetna", in_queue: 4, active: 2 },
        { provider_id: null, provider_name: null, in_queue: 2, active: 0 },
      ],
    })
    render(<LiveAnalyticsPanel />)
    await waitFor(() => expect(screen.getByText("Aetna")).toBeInTheDocument())
    expect(screen.getByText("(No provider)")).toBeInTheDocument()
    const totals = screen.getByTestId("live-totals")
    expect(totals).toHaveTextContent("6")
    expect(totals).toHaveTextContent("2")
  })

  it("shows an empty state when nothing is live", async () => {
    mocked.mockResolvedValue({ rows: [] })
    render(<LiveAnalyticsPanel />)
    await waitFor(() =>
      expect(screen.getByText(/no calls in queue or in progress/i)).toBeInTheDocument(),
    )
  })

  it("surfaces a load error", async () => {
    mocked.mockRejectedValue(new Error("boom"))
    render(<LiveAnalyticsPanel />)
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument())
  })
})
