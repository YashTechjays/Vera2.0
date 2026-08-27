import { useEffect } from "react"
import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import rawSchema from "../../../../vera-backend/data/form_schemas/ibv_form_standard_v2.json"
import {
  getPatientForm,
  getSchemaVersion,
  listInsuranceProviders,
  resolveDisputes,
} from "@/lib/patient-forms/api"
import { IbvProvider, useIbv } from "./IbvProvider"

vi.mock("@/lib/patient-forms/api", () => ({
  createPatientForm: vi.fn(),
  getSchemaVersion: vi.fn(),
  listInsuranceProviders: vi.fn(),
  getPatientForm: vi.fn(),
  resolveDisputes: vi.fn(),
  updatePatientFormStatus: vi.fn(),
}))

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const mockedDetail = vi.mocked(getPatientForm)
const mockedVersion = vi.mocked(getSchemaVersion)
const mockedProviders = vi.mocked(listInsuranceProviders)
const mockedResolve = vi.mocked(resolveDisputes)

const FORM_ID = "33333333-3333-3333-3333-333333333333"
const VERSION_ID = "22222222-2222-2222-2222-222222222222"
const PATIENT_NAME = "sections.patient_information.patient_name"

const DETAIL = {
  id: FORM_ID,
  status: "exception_review" as const,
  insurance_type: "infertility_treatment",
  schema_version_id: VERSION_ID,
  completion_pct: 0,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
  patient_name: "Ava Davis",
  chart_number: null,
  appointment_date: null,
  insurance_provider: null,
  fields: [
    {
      field_path: PATIENT_NAME,
      value: "Ava Davis",
      source: "ai_call" as const,
      confidence: 95,
      evidence: null,
      dispute: {
        previous_value: "Ava D.",
        current_value: "Ava Davis",
        confidence: 95,
        evidence: null,
        reasoning: null,
      },
      provenance: null,
    },
  ],
  ivr_navigation_enabled: false,
  call_scoped_paths: [],
}

function Harness() {
  const {
    openFormById,
    setValue,
    values,
    resolveOpenDisputes,
    disputes,
    flagsFor,
    save,
    loading,
  } = useIbv()
  const pendingDisputeCount = Object.keys(disputes).filter(
    (path) => !flagsFor(path).applied,
  ).length
  useEffect(() => {
    openFormById(FORM_ID)
  }, [openFormById])
  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="value">{values[PATIENT_NAME] ?? ""}</span>
      <span data-testid="pending">{pendingDisputeCount}</span>
      <button type="button" onClick={() => setValue(PATIENT_NAME, "Ava Smith")}>
        edit
      </button>
      <button type="button" onClick={() => resolveOpenDisputes([PATIENT_NAME])}>
        resolve section
      </button>
      <button type="button" onClick={() => void save()}>
        save
      </button>
    </div>
  )
}

async function renderLoaded() {
  render(
    <IbvProvider>
      <Harness />
    </IbvProvider>,
  )
  await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"))
}

describe("section-wise resolve keeps the reviewer's edited value", () => {
  beforeEach(() => {
    mockedDetail.mockReset()
    mockedVersion.mockReset()
    mockedProviders.mockReset()
    mockedResolve.mockReset()
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
    mockedResolve.mockResolvedValue(DETAIL)
  })

  it("an edited value survives the section resolve and is saved as a correction", async () => {
    const user = userEvent.setup({ delay: null })
    await renderLoaded()
    expect(screen.getByTestId("pending")).toHaveTextContent("1")

    await user.click(screen.getByRole("button", { name: "edit" }))
    await user.click(screen.getByRole("button", { name: "resolve section" }))

    // The edit must not be clobbered by the dispute's captured value.
    expect(screen.getByTestId("value")).toHaveTextContent("Ava Smith")
    expect(screen.getByTestId("pending")).toHaveTextContent("0")

    await user.click(screen.getByRole("button", { name: "save" }))
    await waitFor(() => expect(mockedResolve).toHaveBeenCalledTimes(1))
    // The correction rides in form_data; the resolved dispute is still sent as an
    // accept so a normalize-equal edit (case-only) cannot leave the dispute open.
    expect(mockedResolve).toHaveBeenCalledWith(FORM_ID, {
      form_data: { [PATIENT_NAME]: "Ava Smith" },
      dispute_fields: [PATIENT_NAME],
      reasked_fields: [],
    })
  })

  it("an untouched dispute still resolves as an accept of the captured value", async () => {
    const user = userEvent.setup({ delay: null })
    await renderLoaded()

    await user.click(screen.getByRole("button", { name: "resolve section" }))
    expect(screen.getByTestId("value")).toHaveTextContent("Ava Davis")
    expect(screen.getByTestId("pending")).toHaveTextContent("0")

    await user.click(screen.getByRole("button", { name: "save" }))
    await waitFor(() => expect(mockedResolve).toHaveBeenCalledTimes(1))
    expect(mockedResolve).toHaveBeenCalledWith(FORM_ID, {
      form_data: { [PATIENT_NAME]: "Ava Davis" },
      dispute_fields: [PATIENT_NAME],
      reasked_fields: [],
    })
  })
})
