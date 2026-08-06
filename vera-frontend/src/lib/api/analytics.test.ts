import { beforeEach, describe, expect, it, vi } from "vitest"

import { apiRequest } from "@/lib/api/client"
import {
  getHistoryReport,
  getLivePanel,
  getQueueStatus,
  getReportFilters,
} from "@/lib/api/analytics"

vi.mock("@/lib/api/client", () => ({ apiRequest: vi.fn() }))

const mocked = vi.mocked(apiRequest)

describe("analytics api", () => {
  beforeEach(() => {
    mocked.mockReset()
  })

  it("GETs the queue status", async () => {
    const status = { limit: 3, active: 3, in_queue: 2 }
    mocked.mockResolvedValueOnce(status)
    await expect(getQueueStatus()).resolves.toEqual(status)
    expect(mocked).toHaveBeenCalledWith("/analytics/queue-status")
  })

  it("GETs the live panel", async () => {
    mocked.mockResolvedValueOnce({ rows: [] })
    await getLivePanel()
    expect(mocked).toHaveBeenCalledWith("/analytics/live")
  })

  it("GETs the report with only the provided filters", async () => {
    mocked.mockResolvedValueOnce({})
    await getHistoryReport({
      date_from: "2026-01-08T00:00:00.000Z",
      date_to: "2026-01-15T00:00:00.000Z",
      provider_id: "p-1",
    })
    const url = mocked.mock.calls[0][0] as string
    expect(url).toContain("/analytics/report?")
    expect(url).toContain("provider_id=p-1")
    expect(url).not.toContain("va_id")
  })

  it("GETs the filter options", async () => {
    mocked.mockResolvedValueOnce({ providers: [], vas: [] })
    await getReportFilters()
    expect(mocked).toHaveBeenCalledWith("/analytics/filters")
  })
})
