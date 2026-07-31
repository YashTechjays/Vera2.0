import { configureStore } from "@reduxjs/toolkit"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { renderToStaticMarkup } from "react-dom/server"
import { Provider } from "react-redux"
import { beforeEach, describe, expect, it, vi } from "vitest"

// The whole module is mocked, so every function the page or dialogs import must be
// present here — a missing one only surfaces as "x is not a function" at call time.
vi.mock("@/lib/api/platform", () => ({
  createTenant: vi.fn(),
  updateTenant: vi.fn(),
  listTenants: vi.fn().mockResolvedValue([]),
  getTenant: vi.fn(),
  deactivateTenant: vi.fn(),
  reactivateTenant: vi.fn(),
  listTenantUsers: vi.fn().mockResolvedValue([]),
  listTenantRoles: vi.fn().mockResolvedValue([]),
  inviteTenantUser: vi.fn(),
}))

import { TenantFormDialog } from "@/components/platform/TenantFormDialog"
import { createTenant, listTenants, updateTenant, type TenantDetail } from "@/lib/api/platform"
import { PlatformTenants } from "@/pages/PlatformTenants"
import authReducer from "@/store/authSlice"

// Same pattern as PlatformOperators.test.tsx: a real store at its default
// (unauthenticated) state, wrapped in a real react-redux <Provider>.
describe("PlatformTenants", () => {
  beforeEach(() => {
    vi.mocked(listTenants).mockResolvedValue([])
  })

  it("renders the platform-only guard message for a non-super-admin", () => {
    const store = configureStore({ reducer: { auth: authReducer } })
    const html = renderToStaticMarkup(
      <Provider store={store}>
        <PlatformTenants />
      </Provider>,
    )
    expect(html).toContain("only available to platform operators")
    expect(listTenants).not.toHaveBeenCalled()
  })

  it("asks for every status so deactivated tenants stay manageable", async () => {
    // The active-only default exists for the elevation picker; this table needs both,
    // or Deactivate would hide the row and Reactivate could never be reached.
    const store = configureStore({
      reducer: { auth: authReducer },
      preloadedState: {
        auth: {
          ...authReducer(undefined, { type: "@@INIT" }),
          status: "authenticated" as const,
          user: { account_type: "platform" } as never,
        },
      },
    })
    render(
      <Provider store={store}>
        <PlatformTenants />
      </Provider>,
    )
    await waitFor(() => expect(listTenants).toHaveBeenCalledWith({ status: "all" }))
    expect(await screen.findByText("No tenants yet.")).toBeTruthy()
  })
})

const tenant: TenantDetail = {
  id: "t1",
  name: "Acme Health",
  slug: "acme-health",
  status: "active",
  region: "us-east",
  created_at: "2026-07-30T00:00:00Z",
  observer_enabled: true,
  auto_retry_enabled: false,
  retry_fill_threshold: 0.5,
  max_agents_per_va: 3,
  max_concurrent_calls: 25,
  max_retries: 5,
  queue_expiry_hours: 48,
  recording_retention_days: null,
}

describe("TenantFormDialog — create mode", () => {
  beforeEach(() => vi.resetAllMocks())

  it("derives the slug from the name until the operator edits it", async () => {
    render(<TenantFormDialog open onOpenChange={() => {}} tenant={null} />)
    await userEvent.type(screen.getByLabelText("Name"), "Acme Health")
    expect(screen.getByLabelText("Slug")).toHaveValue("acme-health")

    const slug = screen.getByLabelText("Slug")
    await userEvent.clear(slug)
    await userEvent.type(slug, "custom")
    await userEvent.type(screen.getByLabelText("Name"), " Two")
    expect(slug).toHaveValue("custom")
  })

  it("hides the config knobs — create takes identity only", () => {
    render(<TenantFormDialog open onOpenChange={() => {}} tenant={null} />)
    expect(screen.queryByLabelText("Max retries")).toBeNull()
    expect(screen.getByRole("button", { name: "Create tenant" })).toBeTruthy()
  })

  it("refuses an invalid slug without calling the API", async () => {
    render(<TenantFormDialog open onOpenChange={() => {}} tenant={null} />)
    await userEvent.type(screen.getByLabelText("Name"), "Acme")
    const slug = screen.getByLabelText("Slug")
    await userEvent.clear(slug)
    await userEvent.type(slug, "-bad-")
    await userEvent.click(screen.getByRole("button", { name: "Create tenant" }))
    expect(await screen.findByRole("alert")).toHaveTextContent(/Slug must be lowercase/)
    expect(createTenant).not.toHaveBeenCalled()
  })

  it("creates the tenant with the typed identity", async () => {
    vi.mocked(createTenant).mockResolvedValue(tenant)
    const onSaved = vi.fn()
    render(<TenantFormDialog open onOpenChange={() => {}} tenant={null} onSaved={onSaved} />)
    await userEvent.type(screen.getByLabelText("Name"), "Acme Health")
    await userEvent.type(screen.getByLabelText("Region"), "us-east")
    await userEvent.click(screen.getByRole("button", { name: "Create tenant" }))
    await waitFor(() =>
      expect(createTenant).toHaveBeenCalledWith({
        name: "Acme Health",
        slug: "acme-health",
        region: "us-east",
      }),
    )
    expect(onSaved).toHaveBeenCalled()
  })
})

describe("TenantFormDialog — edit mode", () => {
  beforeEach(() => vi.resetAllMocks())

  it("loads the tenant's values and locks the slug", () => {
    render(<TenantFormDialog open onOpenChange={() => {}} tenant={tenant} />)
    expect(screen.getByLabelText("Name")).toHaveValue("Acme Health")
    expect(screen.getByLabelText("Slug")).toBeDisabled()
    expect(screen.getByLabelText("Max retries")).toHaveValue(5)
    expect(screen.getByLabelText("Queue expiry (hours)")).toHaveValue(48)
  })

  it("PATCHes only the field that changed", async () => {
    vi.mocked(updateTenant).mockResolvedValue(tenant)
    render(<TenantFormDialog open onOpenChange={() => {}} tenant={tenant} />)
    const retries = screen.getByLabelText("Max retries")
    await userEvent.clear(retries)
    await userEvent.type(retries, "2")
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))
    await waitFor(() => expect(updateTenant).toHaveBeenCalledWith("t1", { max_retries: 2 }))
  })

  it("closes without calling the API when nothing changed", async () => {
    const onOpenChange = vi.fn()
    render(<TenantFormDialog open onOpenChange={onOpenChange} tenant={tenant} />)
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(updateTenant).not.toHaveBeenCalled()
  })

  it("surfaces a save failure instead of closing", async () => {
    vi.mocked(updateTenant).mockRejectedValue(new Error("boom"))
    const onOpenChange = vi.fn()
    render(<TenantFormDialog open onOpenChange={onOpenChange} tenant={tenant} />)
    await userEvent.clear(screen.getByLabelText("Name"))
    await userEvent.type(screen.getByLabelText("Name"), "Renamed")
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }))
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not save the tenant/)
    expect(onOpenChange).not.toHaveBeenCalledWith(false)
  })
})
