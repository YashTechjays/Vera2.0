import { act, render, screen, waitFor } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { PatientFormDetail, SchemaVersionDetail } from "@/lib/patient-forms/types"

const DETAIL: PatientFormDetail = {
  id: "f1",
  status: "exception_review",
  schema_version_id: "v1",
  patient_name: "Jane Doe",
  chart_number: "CH-1",
  appointment_date: null,
  appointment_type: null,
  member_id: null,
  insurance_provider_id: null,
  insurance_provider_name: null,
  completion_pct: 0,
  intake_payload: {},
  fields: [
    {
      field_path: "sections.s.f",
      value: "Yes",
      source: "ai_call",
      confidence: 60,
      evidence: "Rep: yes.",
      dispute: {
        previous_value: "No",
        current_value: "Yes",
        confidence: 60,
        evidence: "Rep: yes.",
        reasoning: null,
      },
      // The post-call judge graded attempt 1's value at 100.
      provenance: { attempt: 1, mode: "full", judge: { confidence: 100, supported: true } },
    },
  ],
} as unknown as PatientFormDetail

const SCHEMA: SchemaVersionDetail = {
  id: "v1",
  schema_id: "s1",
  version: 1,
  status: "published",
  insurance_type: "infertility_treatment",
  name: "T",
  document: {
    dsl_version: "2.1",
    name: "T",
    sections: { s: { title: "S", role: "collect", fields: { f: { type: "text", title: "F" } } } },
  },
} as unknown as SchemaVersionDetail

vi.mock("@/lib/patient-forms/api", () => ({
  getPatientForm: vi.fn(() => Promise.resolve(DETAIL)),
  getSchemaVersion: vi.fn(() => Promise.resolve(SCHEMA)),
  listInsuranceProviders: vi.fn(() => Promise.resolve([])),
  getPatientFormCalls: vi.fn(() => Promise.resolve([])),
  resolveDisputes: vi.fn(),
  updatePatientFormStatus: vi.fn(),
  exportPatientForm: vi.fn(),
  listPatientForms: vi.fn(),
  listIntakeSchemas: vi.fn(),
  createPatientForm: vi.fn(),
}))

import { IbvProvider, useIbv } from "./IbvProvider"

const PATH = "sections.s.f"

function Probe() {
  const { confidenceFor, provenanceFor, loadFormById, applyLiveAnswer, setValue, formId } = useIbv()
  const c = confidenceFor(PATH)
  return (
    <div>
      <button onClick={() => loadFormById("f1")}>load</button>
      <button onClick={() => setValue(PATH, "Typed")}>edit</button>
      <span data-testid="attempt">{provenanceFor(PATH)?.attempt ?? "none"}</span>
      <button
        onClick={() =>
          applyLiveAnswer("f1", PATH, "Maybe", {
            previousValue: "No",
            currentValue: "Maybe",
            confidence: 72,
            evidence: null,
            reasoning: null,
          })
        }
      >
        live
      </button>
      <span data-testid="src">{formId ? `${c.source}:${c.score}` : "unloaded"}</span>
    </div>
  )
}

// The judge only runs post-call, so its verdict grades the value it saw. Anything that
// replaces that value — a live frame on a retry, or a reviewer typing — makes the score
// a statement about text no longer on screen, and `confidenceFor` prefers the judge.
describe("a replaced value must not inherit the previous judge verdict", () => {
  async function loaded() {
    render(
      <IbvProvider>
        <Probe />
      </IbvProvider>
    )
    act(() => screen.getByText("load").click())
    await waitFor(() => expect(screen.getByTestId("src")).toHaveTextContent("judge:100"))
  }

  it("falls back to the capture score once a live frame replaces the value", async () => {
    await loaded()
    act(() => screen.getByText("live").click())
    await waitFor(() => expect(screen.getByTestId("src")).toHaveTextContent("captured:72"))
  })

  it("falls back the same way when a reviewer retypes the value", async () => {
    await loaded()
    act(() => screen.getByText("edit").click())
    await waitFor(() => expect(screen.getByTestId("src")).toHaveTextContent("captured:60"))
  })

  it("keeps the attempt attribution — a new value does not unsay which call ran", async () => {
    await loaded()
    act(() => screen.getByText("live").click())
    await waitFor(() => expect(screen.getByTestId("attempt")).toHaveTextContent("1"))
  })
})
