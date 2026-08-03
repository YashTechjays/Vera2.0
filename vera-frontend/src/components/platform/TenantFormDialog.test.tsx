import { describe, it, expect, vi } from "vitest"
import { render, screen } from "@testing-library/react"
import { TenantFormDialog } from "./TenantFormDialog"

describe("TenantFormDialog", () => {
  it("renders the retry threshold label with new text when editing a tenant", () => {
    const mockTenant = {
      id: "test-id",
      name: "Test Tenant",
      slug: "test-tenant",
      status: "active" as const,
      region: null,
      created_at: "2024-01-01T00:00:00Z",
      observer_enabled: true,
      auto_retry_enabled: true,
      retry_fill_threshold: 0.5,
      max_agents_per_va: 3,
      max_concurrent_calls: 25,
      max_retries: 5,
      queue_expiry_hours: 48,
      recording_retention_days: null,
    }

    render(
      <TenantFormDialog
        open={true}
        onOpenChange={vi.fn()}
        tenant={mockTenant}
      />,
    )

    const label = screen.getByText("Min verified fraction before review (0–1)")
    expect(label).toBeInTheDocument()
  })
})
