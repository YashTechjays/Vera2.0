import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api/platform", () => ({
  listTenantUsers: vi.fn(),
  listTenantRoles: vi.fn(),
  inviteTenantUser: vi.fn(),
}))

import { TenantUsersDialog } from "@/components/platform/TenantUsersDialog"
import {
  inviteTenantUser,
  listTenantRoles,
  listTenantUsers,
  type TenantSummary,
} from "@/lib/api/platform"

const tenant: TenantSummary = {
  id: "t1",
  name: "Acme Health",
  slug: "acme-health",
  status: "active",
  region: null,
  created_at: "2026-07-30T00:00:00Z",
  observer_enabled: true,
  auto_retry_enabled: true,
  retry_fill_threshold: 0.5,
}

const ROLES = [{ id: "r-admin", name: "TENANT_ADMIN", is_system: true }]

describe("TenantUsersDialog", () => {
  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(listTenantRoles).mockResolvedValue(ROLES)
  })

  it("renders nothing and fetches nothing when no tenant is selected", () => {
    // screen queries search document.body, so they see portalled dialog content —
    // container.textContent would be "" even for an OPEN dialog.
    render(<TenantUsersDialog tenant={null} onClose={() => {}} />)
    expect(screen.queryByText(/Users in/)).toBeNull()
    expect(listTenantUsers).not.toHaveBeenCalled()
  })

  it("loads that tenant's users and shows their role and status", async () => {
    vi.mocked(listTenantUsers).mockResolvedValue([
      { id: "u1", email: "admin@acme.com", name: "Ann", status: "active", roles: ["TENANT_ADMIN"] },
    ])
    render(<TenantUsersDialog tenant={tenant} onClose={() => {}} />)
    expect(await screen.findByText("Ann")).toBeTruthy()
    expect(screen.getByText("admin@acme.com")).toBeTruthy()
    expect(screen.getByText("TENANT_ADMIN")).toBeTruthy()
    expect(screen.getByText("active")).toBeTruthy()
    expect(listTenantUsers).toHaveBeenCalledWith("t1")
  })

  it("prompts for the first admin when the tenant has no users", async () => {
    vi.mocked(listTenantUsers).mockResolvedValue([])
    render(<TenantUsersDialog tenant={tenant} onClose={() => {}} />)
    expect(await screen.findByText(/invite the first admin/i)).toBeTruthy()
  })

  it("invites a user into this tenant with the chosen role", async () => {
    vi.mocked(listTenantUsers).mockResolvedValue([])
    vi.mocked(inviteTenantUser).mockResolvedValue({
      user_id: "u9",
      email: "new@acme.com",
      invite_url: "https://app.example/tenants/acme-health/accept-invite?token=x",
      email_sent: true,
    })
    render(<TenantUsersDialog tenant={tenant} onClose={() => {}} />)

    await userEvent.click(await screen.findByRole("button", { name: "Invite user" }))
    await userEvent.type(screen.getByLabelText("Email"), "new@acme.com")
    await userEvent.type(screen.getByLabelText("Name"), "New Person")
    await userEvent.selectOptions(screen.getByLabelText("Role"), "r-admin")
    await userEvent.click(screen.getByRole("button", { name: "Send invitation" }))

    await waitFor(() =>
      expect(inviteTenantUser).toHaveBeenCalledWith("t1", {
        email: "new@acme.com",
        name: "New Person",
        roleIds: ["r-admin"],
        sendEmail: true,
      }),
    )
    // The invite link is shown so the operator can pass it on out of band.
    expect(await screen.findByLabelText("Invite link")).toHaveValue(
      "https://app.example/tenants/acme-health/accept-invite?token=x",
    )
  })

  it("surfaces an invite failure and keeps the form open", async () => {
    vi.mocked(listTenantUsers).mockResolvedValue([])
    vi.mocked(inviteTenantUser).mockRejectedValue(new Error("boom"))
    render(<TenantUsersDialog tenant={tenant} onClose={() => {}} />)

    await userEvent.click(await screen.findByRole("button", { name: "Invite user" }))
    await userEvent.type(screen.getByLabelText("Email"), "new@acme.com")
    await userEvent.click(screen.getByRole("button", { name: "Send invitation" }))

    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not send the invitation/)
    expect(screen.getByLabelText("Email")).toBeTruthy()
  })

  it("reports a load failure instead of an empty list", async () => {
    vi.mocked(listTenantUsers).mockRejectedValue(new Error("boom"))
    render(<TenantUsersDialog tenant={tenant} onClose={() => {}} />)
    expect(await screen.findByRole("alert")).toHaveTextContent(/Could not load this tenant's users/)
  })
})
