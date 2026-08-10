import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

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

describe("IbvFormModal IVR toggle", () => {
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

  it("shows the toggle unchecked by default and enqueues with IVR off", async () => {
    const { user, queueButton } = await openToQueueButton()
    expect(screen.getByRole("switch")).not.toBeChecked()

    await user.click(queueButton)
    await waitFor(() =>
      expect(mockedStatus).toHaveBeenCalledWith(FORM_ID, "in_queue", {
        enableIvrNavigation: false,
        insuranceProviderId: undefined,
      }),
    )
  })

  it("sends IVR on when the user turns the toggle on", async () => {
    const { user, queueButton } = await openToQueueButton()

    await user.click(screen.getByRole("switch"))
    await user.click(queueButton)

    await waitFor(() =>
      expect(mockedStatus).toHaveBeenCalledWith(FORM_ID, "in_queue", {
        enableIvrNavigation: true,
        insuranceProviderId: undefined,
      }),
    )
  })

  it("pre-checks the toggle from a form stored with IVR enabled", async () => {
    mockedDetail.mockResolvedValue({ ...DETAIL, ivr_navigation_enabled: true })
    await openToQueueButton()
    expect(screen.getByRole("switch")).toBeChecked()
  })
})
