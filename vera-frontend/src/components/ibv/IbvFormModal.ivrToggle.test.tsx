import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import rawSchema from "../../../../vera-backend/data/form_schemas/ibv_form_standard_v2.json"
import {
  getPatientForm,
  getSchemaVersion,
  listInsuranceProviders,
  updatePatientFormStatus,
} from "@/lib/patient-forms/api"
import { IbvFormModal } from "./IbvFormModal"
import { IbvProvider, useIbv } from "./IbvProvider"

vi.mock("@/lib/patient-forms/api", () => ({
  createPatientForm: vi.fn(),
  getSchemaVersion: vi.fn(),
  listIntakeSchemas: vi.fn(),
  listInsuranceProviders: vi.fn(),
  getPatientForm: vi.fn(),
  exportPatientForm: vi.fn(),
  resolveDisputes: vi.fn(),
  updatePatientFormStatus: vi.fn(),
}))

vi.mock("@/lib/auth/permissions", () => ({ usePermission: () => true }))

// Mounts the real 204-leaf IBV document through the provider (see the note in
// CreatePatientFormModal.test.tsx) — over vitest's 5s default on a loaded runner.
vi.setConfig({ testTimeout: 30_000 })

const mockedDetail = vi.mocked(getPatientForm)
const mockedVersion = vi.mocked(getSchemaVersion)
const mockedProviders = vi.mocked(listInsuranceProviders)
const mockedStatus = vi.mocked(updatePatientFormStatus)

const FORM_ID = "33333333-3333-3333-3333-333333333333"
const VERSION_ID = "22222222-2222-2222-2222-222222222222"

const DETAIL = {
  id: FORM_ID,
  status: "ready_for_processing" as const,
  insurance_type: "infertility_treatment",
  schema_version_id: VERSION_ID,
  completion_pct: 0,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
  patient_name: null,
  chart_number: null,
  appointment_date: null,
  insurance_provider: null,
  fields: [],
  // Stored opt-out from an earlier internal test — the hidden-toggle path must override it.
  ivr_navigation_enabled: false,
}

function OpenForm() {
  const { openFormById } = useIbv()
  return (
    <button type="button" onClick={() => openFormById(FORM_ID)}>
      Open form
    </button>
  )
}

/** Open the loaded form modal and return the enqueue button. */
async function openToQueueButton() {
  const user = userEvent.setup({ delay: null })
  render(
    <IbvProvider>
      <OpenForm />
      <IbvFormModal />
    </IbvProvider>,
  )
  await user.click(screen.getByRole("button", { name: "Open form" }))
  return { user, queueButton: await screen.findByRole("button", { name: "Send to queue" }) }
}

describe("IbvFormModal IVR toggle dev flag", () => {
  beforeEach(() => {
    mockedDetail.mockReset()
    mockedVersion.mockReset()
    mockedProviders.mockReset()
    mockedStatus.mockReset()
    mockedDetail.mockResolvedValue(DETAIL)
    mockedVersion.mockResolvedValue({
      id: VERSION_ID,
      schema_id: "11111111-1111-1111-1111-111111111111",
      version: 3,
      status: "published",
      insurance_type: "infertility_treatment",
      name: "IBV Form Standard",
      document: rawSchema,
    })
    mockedProviders.mockResolvedValue([])
    mockedStatus.mockResolvedValue({ id: FORM_ID, status: "in_queue" })
  })

  afterEach(() => localStorage.removeItem("vera:show-ivr-toggle"))

  it("hides the toggle and forces IVR navigation ON when enqueueing", async () => {
    const { user, queueButton } = await openToQueueButton()
    expect(screen.queryByText("IVR navigation")).not.toBeInTheDocument()

    await user.click(queueButton)

    await waitFor(() =>
      expect(mockedStatus).toHaveBeenCalledWith(FORM_ID, "in_queue", {
        enableIvrNavigation: true, // stored opt-out overridden while hidden
        insuranceProviderId: undefined,
      }),
    )
  })

  it("shows the toggle and honors it when the dev flag is set", async () => {
    localStorage.setItem("vera:show-ivr-toggle", "true")
    const { user, queueButton } = await openToQueueButton()
    expect(screen.getByText("IVR navigation")).toBeInTheDocument()

    await user.click(queueButton)

    await waitFor(() =>
      expect(mockedStatus).toHaveBeenCalledWith(FORM_ID, "in_queue", {
        enableIvrNavigation: false, // the stored value, untouched
        insuranceProviderId: undefined,
      }),
    )
  })
})
