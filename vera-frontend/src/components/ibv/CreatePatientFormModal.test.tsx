import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { toast } from "sonner"
import { beforeEach, describe, expect, it, vi } from "vitest"

import rawSchema from "../../../../vera-backend/data/form_schemas/ibv_form_standard_v2.json"
import { createRequiredPaths, parseSchema } from "@/lib/ibv/schema"
import { ApiError } from "@/lib/api/errors"
import { createPatientForm, getSchemaVersion, listIntakeSchemas } from "@/lib/patient-forms/api"
import { INVALID_LOOK } from "./FieldRenderer"
import { CreatePatientFormModal } from "./CreatePatientFormModal"
import { IbvProvider, useIbv } from "./IbvProvider"

vi.mock("@/lib/patient-forms/api", () => ({
  createPatientForm: vi.fn(),
  getSchemaVersion: vi.fn(),
  listIntakeSchemas: vi.fn(),
  listInsuranceProviders: vi.fn(),
  getPatientForm: vi.fn(),
  resolveDisputes: vi.fn(),
  updatePatientFormStatus: vi.fn(),
}))

vi.mock("sonner", () => ({ toast: { success: vi.fn() } }))

// These mount the real 204-leaf IBV document and drive it through the provider, so
// they cost ~0.5s each locally and ~5.5s on a loaded CI runner — over vitest's 5s
// unit-test default, which failed the pipeline on build 82b7c1f5.
vi.setConfig({ testTimeout: 30_000 })

const mockedList = vi.mocked(listIntakeSchemas)
const mockedVersion = vi.mocked(getSchemaVersion)
const mockedCreate = vi.mocked(createPatientForm)

const SCHEMA_ID = "11111111-1111-1111-1111-111111111111"
const VERSION_ID = "22222222-2222-2222-2222-222222222222"

const OPTION = {
  schema_id: SCHEMA_ID,
  name: "IBV Form Standard",
  insurance_type: "infertility_treatment",
  published_version_id: VERSION_ID,
  published_version: 3,
}

/** The create modal only opens through the provider, so drive it from a child. */
function OpenCreate() {
  const { openCreate } = useIbv()
  return (
    <button type="button" onClick={openCreate}>
      Add patient form
    </button>
  )
}

function renderModal() {
  return render(
    <IbvProvider>
      <OpenCreate />
      <CreatePatientFormModal />
    </IbvProvider>,
  )
}

/** Open the picker, choose the only family, and land on the filled form step. */
async function reachFormStep(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Add patient form" }))
  await waitFor(() => expect(mockedList).toHaveBeenCalled())
  await user.selectOptions(
    await screen.findByRole("combobox"),
    screen.getByRole("option", { name: /IBV Form Standard/ }),
  )
  await user.click(screen.getByRole("button", { name: "Continue" }))
  await screen.findByRole("button", { name: "Submit" })
}

/** Seed every create-required leaf through its input. `fireEvent.change`, not
 *  `user.type`: this is state setup, not a typing test, and 16 fields
 *  keystroke-by-keystroke costs several times more. */
function fillRequired() {
  for (const path of createRequiredPaths(parseSchema(rawSchema))) {
    const value = SAMPLE_VALUES[path]
    expect(value, `no sample value for ${path}`).toBeDefined()
    const input = document
      .querySelector(`[data-field-path="${path}"]`)
      ?.querySelector("input, select")
    expect(input, path).not.toBeNull()
    fireEvent.change(input as HTMLElement, { target: { value } })
  }
}

/** Open the modal, pick the family, and (optionally) fill the required fields. */
async function atFormStep({ filled = false } = {}) {
  const user = userEvent.setup({ delay: null })
  renderModal()
  await reachFormStep(user)
  if (filled) fillRequired()
  return user
}

describe("CreatePatientFormModal", () => {
  beforeEach(() => {
    vi.mocked(toast.success).mockReset()
    mockedList.mockReset()
    mockedVersion.mockReset()
    mockedCreate.mockReset()
    mockedList.mockResolvedValue([OPTION])
    mockedVersion.mockResolvedValue({
      id: VERSION_ID,
      schema_id: SCHEMA_ID,
      version: 3,
      status: "published",
      insurance_type: "infertility_treatment",
      name: "IBV Form Standard",
      document: rawSchema,
    })
  })

  it("labels each family with its published version", async () => {
    const user = userEvent.setup({ delay: null })
    renderModal()
    await user.click(screen.getByRole("button", { name: "Add patient form" })) // picker only
    expect(await screen.findByRole("option", { name: /· v3$/ })).toBeInTheDocument()
  })

  it("names the blocking fields instead of a bare 'required fields' banner", async () => {
    const user = await atFormStep()

    await user.click(screen.getByRole("button", { name: "Submit" }))

    const banner = await screen.findByRole("alert")
    expect(banner).toHaveTextContent(/Chart Number/)
    expect(banner).toHaveTextContent(/and \d+ more/)
    expect(mockedCreate).not.toHaveBeenCalled()
  })

  it("marks the system fields required, not the voice-collected ones", async () => {
    await atFormStep()

    // chart_number is a system field with no default → required at create.
    const chartRow = document.querySelector(
      '[data-field-path="sections.patient_information.chart_number"]',
    )
    expect(chartRow?.textContent).toContain("*")
    // plan_type carries the leaf's own `required` (voice collection) but is not a
    // system field, so it must NOT be marked in create mode.
    const planRow = document.querySelector(
      '[data-field-path="sections.insurance_information.plan_type"]',
    )
    expect(planRow).not.toBeNull()
    expect(planRow?.textContent).not.toContain("*")
  })

  it("sends the rendered version id and an idempotent create, then toasts", async () => {
    mockedCreate.mockResolvedValue({
      id: "33333333-3333-3333-3333-333333333333",
      status: "ready_for_processing",
      insurance_type: "infertility_treatment",
      schema_version_id: VERSION_ID,
      completion_pct: 0,
      created_at: "2026-07-30T00:00:00Z",
    })
    const user = await atFormStep({ filled: true })
    await user.click(screen.getByRole("button", { name: "Submit" }))

    await waitFor(() => expect(mockedCreate).toHaveBeenCalledTimes(1))
    const [schemaId, versionId, payload] = mockedCreate.mock.calls[0]
    expect(schemaId).toBe(SCHEMA_ID)
    expect(versionId).toBe(VERSION_ID) // the version the form was rendered from
    expect(payload).toMatchObject({
      patient_information: { chart_number: "CH-1", patient_name: "Jane Doe" },
    })
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Patient form created."))
  })

  it("surfaces a 409 stale-version conflict as the modal banner", async () => {
    mockedCreate.mockRejectedValue(
      new ApiError(409, "CONFLICT", "a newer version of this form schema has been published"),
    )
    const user = await atFormStep({ filled: true })
    await user.click(screen.getByRole("button", { name: "Submit" }))

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/newer version/),
    )
  })

  it("outlines the paths a backend 422 named", async () => {
    mockedCreate.mockRejectedValue(
      new ApiError(422, "VALIDATION_ERROR", "missing required fields", {
        fields: ["sections.patient_information.patient_dob"],
      }),
    )
    const user = await atFormStep({ filled: true })
    await user.click(screen.getByRole("button", { name: "Submit" }))

    // The reason renders as a tooltip, so the observable signal is the invalid look.
    await waitFor(() => {
      const row = document.querySelector(
        '[data-field-path="sections.patient_information.patient_dob"]',
      )
      expect(row?.querySelector("input")?.className).toContain(INVALID_LOOK)
    })
  })
})

/** A sample value per create-required leaf. The SET of required paths comes from
 *  `createRequiredPaths` so it can never drift from the schema's system_fields;
 *  `fillRequired` asserts this map still covers it. */
const SAMPLE_VALUES: Record<string, string> = {
  "sections.patient_information.chart_number": "CH-1",
  "sections.patient_information.patient_name": "Jane Doe",
  "sections.patient_information.patient_dob": "4/12/1990",
  "sections.patient_information.patient_gender": "Female",
  "sections.appointment_information.appointment_date": "8/3/2026",
  "sections.insurance_information.policy_number": "POL-1",
  "sections.insurance_reference_information.insurance_provider_name": "Demo",
  "sections.insurance_reference_information.insurance_phone_number": "5550100",
  "sections.verification_information.verified_by": "Dr. Reyes",
  "sections.verification_information.callback_number": "5550199",
  "sections.hospital_information.hospital_name": "Demo Health",
  "sections.hospital_information.hospital_address": "123 Demo St",
  "sections.hospital_information.tax_id": "987654313",
  "sections.hospital_information.npi": "1234567893",
  "sections.provider_reference_information.provider_name": "Dr. Smith",
  "sections.provider_reference_information.npi": "1982736450",
}
