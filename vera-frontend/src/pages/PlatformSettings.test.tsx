import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { listTenants, setTenantObserverEnabled, setTenantRetryConfig } from "@/lib/api/platform"
import type { TenantSummary } from "@/lib/api/platform"
import { usePermission } from "@/lib/auth/permissions"
import { PlatformSettings } from "@/pages/PlatformSettings"

vi.mock("@/lib/api/platform", () => ({
  listTenants: vi.fn(),
  setTenantObserverEnabled: vi.fn(),
  setTenantRetryConfig: vi.fn(),
}))

vi.mock("@/lib/auth/permissions", () => ({
  usePermission: vi.fn(),
}))

const mockedListTenants = vi.mocked(listTenants)
const mockedSetTenantObserverEnabled = vi.mocked(setTenantObserverEnabled)
const mockedSetTenantRetryConfig = vi.mocked(setTenantRetryConfig)
const mockedUsePermission = vi.mocked(usePermission)

const tenant: TenantSummary = {
  id: "t1",
  name: "Acme",
  slug: "acme",
  observer_enabled: true,
  auto_retry_enabled: false,
  retry_fill_threshold: 0.5,
}

describe("PlatformSettings", () => {
  beforeEach(() => {
    mockedListTenants.mockReset()
    mockedSetTenantObserverEnabled.mockReset()
    mockedSetTenantRetryConfig.mockReset()
    mockedUsePermission.mockReset()
  })

  it("withholds the surface from a caller lacking the permission", () => {
    mockedUsePermission.mockReturnValue(false)
    mockedListTenants.mockReturnValue(new Promise(() => {}))
    render(<PlatformSettings />)
    expect(screen.getByText(/do not have permission/i)).toBeInTheDocument()
  })

  it("renders the auto-retry switch unchecked and threshold input showing 50", async () => {
    mockedUsePermission.mockReturnValue(true)
    mockedListTenants.mockResolvedValue([tenant])
    render(<PlatformSettings />)

    await waitFor(() => {
      expect(screen.getByRole("switch", { name: /auto retry for acme/i })).not.toBeChecked()
    })
    expect(screen.getByLabelText(/retry threshold for acme/i)).toHaveValue(50)
  })

  it("toggling the switch calls setTenantRetryConfig with auto_retry_enabled", async () => {
    mockedUsePermission.mockReturnValue(true)
    mockedListTenants.mockResolvedValue([tenant])
    mockedSetTenantRetryConfig.mockResolvedValue({
      tenant_id: "t1",
      auto_retry_enabled: true,
      retry_fill_threshold: 0.5,
    })
    render(<PlatformSettings />)

    const toggle = await screen.findByRole("switch", { name: /auto retry for acme/i })
    await userEvent.click(toggle)

    await waitFor(() => {
      expect(mockedSetTenantRetryConfig).toHaveBeenCalledWith("t1", { auto_retry_enabled: true })
    })
    expect(toggle).toBeChecked()
  })

  it("reverts the optimistic switch and shows the error on a rejected call", async () => {
    mockedUsePermission.mockReturnValue(true)
    mockedListTenants.mockResolvedValue([tenant])
    mockedSetTenantRetryConfig.mockRejectedValue(new Error("boom"))
    render(<PlatformSettings />)

    const toggle = await screen.findByRole("switch", { name: /auto retry for acme/i })
    await userEvent.click(toggle)

    await waitFor(() => expect(toggle).not.toBeChecked())
    expect(screen.getByRole("alert")).toBeInTheDocument()
  })

  it("changing the threshold to 40 and blurring calls setTenantRetryConfig with 0.4", async () => {
    mockedUsePermission.mockReturnValue(true)
    mockedListTenants.mockResolvedValue([tenant])
    mockedSetTenantRetryConfig.mockResolvedValue({
      tenant_id: "t1",
      auto_retry_enabled: false,
      retry_fill_threshold: 0.4,
    })
    render(<PlatformSettings />)

    const input = await screen.findByLabelText(/retry threshold for acme/i)
    await userEvent.clear(input)
    await userEvent.type(input, "40")
    await userEvent.tab()

    await waitFor(() => {
      expect(mockedSetTenantRetryConfig).toHaveBeenCalledWith("t1", { retry_fill_threshold: 0.4 })
    })
  })
})
