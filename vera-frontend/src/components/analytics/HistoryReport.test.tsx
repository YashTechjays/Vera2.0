import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { HistoryReport } from "@/components/analytics/HistoryReport"
import { getHistoryReport, getReportFilters } from "@/lib/api/analytics"

vi.mock("@/lib/api/analytics", () => ({
  getHistoryReport: vi.fn(),
  getReportFilters: vi.fn(),
}))

const mockedReport = vi.mocked(getHistoryReport)
const mockedFilters = vi.mocked(getReportFilters)

const REPORT = {
  current: {
    call_volume: 40,
    avg_duration_seconds: 360,
    avg_completion_pct: 53.3,
    intervened_calls: 10,
    intervention_rate: 0.25,
  },
  previous: {
    call_volume: 20,
    avg_duration_seconds: 300,
    avg_completion_pct: 50,
    intervened_calls: 8,
    intervention_rate: 0.4,
  },
  calls_per_day: [{ day: "2026-08-01", calls: 40 }],
  interventions_by_type: [{ type: "whisper", count: 10 }],
  interventions_per_day: [{ day: "2026-08-01", flag: 0, coach: 3, whisper: 7, takeover: 0 }],
}

describe("HistoryReport", () => {
  beforeEach(() => {
    mockedReport.mockReset()
    mockedFilters.mockReset()
    mockedReport.mockResolvedValue(REPORT)
    mockedFilters.mockResolvedValue({
      providers: [{ id: "p1", name: "Aetna" }],
      vas: [{ id: "u1", name: "Sam VA" }],
    })
  })

  it("loads and renders the metric cards", async () => {
    render(<HistoryReport />)
    await waitFor(() => expect(screen.getByText("40")).toBeInTheDocument())
    expect(screen.getByText("Data Capture %")).toBeInTheDocument()
    expect(screen.getByText("53.3%")).toBeInTheDocument()
    expect(screen.getByText("Intervention Rate")).toBeInTheDocument()
    expect(screen.getByText("25.0%")).toBeInTheDocument()
    expect(screen.getByText("6m 0s")).toBeInTheDocument()
  })

  it("renders the interventions-per-day chart card", async () => {
    render(<HistoryReport />)
    await waitFor(() => expect(screen.getByText("Interventions per day")).toBeInTheDocument())
    expect(screen.queryByText("No interventions in this period")).not.toBeInTheDocument()
  })

  it("shows an empty state when the range has no interventions", async () => {
    mockedReport.mockResolvedValue({ ...REPORT, interventions_per_day: [] })
    render(<HistoryReport />)
    await waitFor(() =>
      expect(screen.getByText("No interventions in this period")).toBeInTheDocument(),
    )
  })

  it("both charts show an empty state when the range has zero calls (VR2-284)", async () => {
    mockedReport.mockResolvedValue({
      ...REPORT,
      current: { ...REPORT.current, call_volume: 0, intervened_calls: 0, intervention_rate: null },
      // The backend zero-fills days (VR2-282), so an empty window still carries rows.
      calls_per_day: [
        { day: "2026-08-01", calls: 0 },
        { day: "2026-08-02", calls: 0 },
      ],
      interventions_by_type: [],
      interventions_per_day: [],
    })
    render(<HistoryReport />)
    await waitFor(() => expect(screen.getByText("No calls in this period")).toBeInTheDocument())
    expect(screen.getByText("No interventions in this period")).toBeInTheDocument()
  })

  it("calls chart shows no empty state while the range has calls", async () => {
    render(<HistoryReport />)
    await waitFor(() => expect(screen.getByText("Calls per day")).toBeInTheDocument())
    expect(screen.queryByText("No calls in this period")).not.toBeInTheDocument()
  })

  it("refetches when the provider filter changes", async () => {
    render(<HistoryReport />)
    await waitFor(() => expect(mockedReport).toHaveBeenCalledTimes(1))
    await userEvent.selectOptions(screen.getByLabelText(/provider/i), "p1")
    await waitFor(() => expect(mockedReport).toHaveBeenCalledTimes(2))
    expect(mockedReport.mock.calls[1][0]).toMatchObject({ provider_id: "p1" })
  })

  it("shows custom date inputs only for the custom preset", async () => {
    render(<HistoryReport />)
    await waitFor(() => expect(mockedReport).toHaveBeenCalled())
    expect(screen.queryByLabelText(/from date/i)).not.toBeInTheDocument()
    await userEvent.selectOptions(screen.getByLabelText(/date range/i), "custom")
    expect(screen.getByLabelText(/from date/i)).toBeInTheDocument()
  })
})
