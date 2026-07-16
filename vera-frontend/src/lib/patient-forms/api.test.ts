import { beforeEach, describe, expect, it, vi } from "vitest"

// Factory mock (not auto-mock): the real client imports auth/storage, which
// touches sessionStorage at module load — undefined in the node test env.
vi.mock("@/lib/api/client", () => ({ apiRequest: vi.fn() }))

import { apiRequest } from "@/lib/api/client"
import { listInsuranceProviders, updatePatientFormStatus } from "./api"

describe("patient-forms API client", () => {
  beforeEach(() => vi.resetAllMocks())

  it("lists active insurance providers with GET /patient-forms/insurance-providers", async () => {
    const providers = [{ id: "p1", name: "Cigna" }]
    vi.mocked(apiRequest).mockResolvedValue(providers)
    const out = await listInsuranceProviders()
    expect(out).toEqual(providers)
    expect(apiRequest).toHaveBeenCalledWith("/patient-forms/insurance-providers")
  })

  it("sends the picked provider id (and IVR toggle) on an in_queue change", async () => {
    vi.mocked(apiRequest).mockResolvedValue({ id: "f1", status: "in_queue" })
    await updatePatientFormStatus("f1", "in_queue", {
      enableIvrNavigation: true,
      insuranceProviderId: "p1",
    })
    expect(apiRequest).toHaveBeenCalledWith("/patient-forms/f1/status", {
      method: "PUT",
      body: {
        status: "in_queue",
        enable_ivr_navigation: true,
        insurance_provider_id: "p1",
      },
    })
  })

  it("omits insurance_provider_id when no provider is picked", async () => {
    vi.mocked(apiRequest).mockResolvedValue({ id: "f1", status: "in_queue" })
    await updatePatientFormStatus("f1", "in_queue", { enableIvrNavigation: false })
    expect(apiRequest).toHaveBeenCalledWith("/patient-forms/f1/status", {
      method: "PUT",
      body: { status: "in_queue", enable_ivr_navigation: false },
    })
  })
})
