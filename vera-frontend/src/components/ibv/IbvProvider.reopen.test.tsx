import { render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import rawSchema from "../../../../vera-backend/data/form_schemas/ibv_form_standard_v2.json"
import {
  getPatientForm,
  getSchemaVersion,
  listInsuranceProviders,
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

const FORM_ID = "44444444-4444-4444-4444-444444444444"
const VERSION_ID = "55555555-5555-5555-5555-555555555555"
const REP_NAME = "sections.insurance_representative.rep_name"
const UNVERIFIED_FIELD = "sections.insurance_information.plan_name"

const DETAIL = {
  id: FORM_ID,
  status: "in_call" as const,
  insurance_type: "infertility_treatment",
  schema_version_id: VERSION_ID,
  completion_pct: 0,
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:00Z",
  patient_name: "Dana Whitfield",
  chart_number: null,
  appointment_date: null,
  insurance_provider: null,
  fields: [
    {
      field_path: UNVERIFIED_FIELD,
      value: "Aetna PPO",
      source: "ai_call" as const,
      confidence: 91,
      evidence: null,
      dispute: null,
      // authoritative:false is what FieldRow renders the "Unverified" pill from; the judge
      // verdict is what confidenceFor prefers over the capture score.
      provenance: {
        attempt: 1,
        mode: "full" as const,
        judge: { confidence: 44, supported: false },
        authoritative: false,
      },
    },
  ],
  ivr_navigation_enabled: false,
  call_scoped_paths: [REP_NAME],
}

/**
 * Live Monitoring's exact sequence. It loads the form ONCE (`loadFormById`, on expanding the
 * inline panel) and then reopens the modal over the already-loaded form (`openLoadedForm`)
 * without refetching — `LiveMonitoring.tsx:470` takes that branch whenever the form is already
 * loaded and the call has not ended. So anything `closeForm` throws away is gone for the rest
 * of the session, while `formId`, `schema` and `values` deliberately survive.
 */
function Harness() {
  const { loadFormById, openLoadedForm, closeForm, callScopedPaths, schema, provenanceFor } =
    useIbv()
  const prov = provenanceFor(UNVERIFIED_FIELD)
  return (
    <div>
      <span data-testid="scoped">{callScopedPaths.size}</span>
      <span data-testid="authoritative">{prov === null ? "absent" : String(prov.authoritative)}</span>
      <span data-testid="schema">{schema ? "loaded" : "none"}</span>
      <button type="button" onClick={() => loadFormById(FORM_ID)}>
        expand panel
      </button>
      <button type="button" onClick={openLoadedForm}>
        open modal
      </button>
      <button type="button" onClick={closeForm}>
        close modal
      </button>
    </div>
  )
}

describe("reopening an already-loaded form from Live Monitoring", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedDetail.mockResolvedValue(DETAIL)
    mockedVersion.mockResolvedValue({
      id: VERSION_ID,
      schema_id: "66666666-6666-6666-6666-666666666666",
      version: 2,
      status: "published",
      insurance_type: "infertility_treatment",
      name: "IBV Form Standard",
      document: rawSchema,
    })
    mockedProviders.mockResolvedValue([])
  })

  it("keeps the call-scoped set across close and reopen", async () => {
    const user = userEvent.setup()
    render(
      <IbvProvider>
        <Harness />
      </IbvProvider>,
    )

    await user.click(screen.getByText("expand panel"))
    await waitFor(() => expect(screen.getByTestId("schema")).toHaveTextContent("loaded"))
    expect(screen.getByTestId("scoped")).toHaveTextContent("1")

    await user.click(screen.getByText("open modal"))
    await user.click(screen.getByText("close modal"))
    await user.click(screen.getByText("open modal"))

    // The form itself deliberately survives closeForm so the reopen needs no refetch...
    expect(screen.getByTestId("schema")).toHaveTextContent("loaded")
    // ...so anything derived from that same load must survive with it. Clearing this while
    // keeping the form loaded made the per-call tint and its legend row vanish on reopen and
    // never come back, because Live Monitoring reopens via openLoadedForm and never refetches.
    expect(screen.getByTestId("scoped")).toHaveTextContent("1")
    expect(mockedDetail).toHaveBeenCalledTimes(1)
  })

  it("keeps field provenance across close and reopen", async () => {
    const user = userEvent.setup()
    render(
      <IbvProvider>
        <Harness />
      </IbvProvider>,
    )

    await user.click(screen.getByText("expand panel"))
    await waitFor(() => expect(screen.getByTestId("schema")).toHaveTextContent("loaded"))
    expect(screen.getByTestId("authoritative")).toHaveTextContent("false")

    await user.click(screen.getByText("open modal"))
    await user.click(screen.getByText("close modal"))
    await user.click(screen.getByText("open modal"))

    // Same reasoning as the call-scoped set above, and the same reopen path: dropping this
    // while keeping the form loaded took the FieldRow "Unverified" pill and confidenceFor's
    // judge verdict out for the rest of the session, since Live Monitoring reopens through
    // openLoadedForm and never refetches. "absent" here is that regression.
    expect(screen.getByTestId("schema")).toHaveTextContent("loaded")
    expect(screen.getByTestId("authoritative")).toHaveTextContent("false")
    expect(mockedDetail).toHaveBeenCalledTimes(1)
  })
})
