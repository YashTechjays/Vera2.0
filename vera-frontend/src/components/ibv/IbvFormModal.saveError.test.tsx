import { useEffect } from "react"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"

import rawSchema from "../../../../vera-backend/data/form_schemas/ibv_form_standard_v2.json"
import { ApiError } from "@/lib/api/client"
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
  listIntakeSchemas: vi.fn(),
  listInsuranceProviders: vi.fn(),
  getPatientForm: vi.fn(),
  exportPatientForm: vi.fn(),
  resolveDisputes: vi.fn(),
  updatePatientFormStatus: vi.fn(),
}))

const toastError = vi.hoisted(() => vi.fn())
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: toastError } }))

const mockedDetail = vi.mocked(getPatientForm)
const mockedVersion = vi.mocked(getSchemaVersion)
const mockedProviders = vi.mocked(listInsuranceProviders)
const mockedResolve = vi.mocked(resolveDisputes)

const FORM_ID = "33333333-3333-3333-3333-333333333333"
const VERSION_ID = "22222222-2222-2222-2222-222222222222"
const PLAN_EFFECTIVE_DATE = "sections.benefit_coverage.plan_effective_date"

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
  call_scoped_paths: [],
}

/** Drives IbvProvider.save() directly — lighter than mounting the full modal, and
 *  exercises the exact code path VR2-187's fix changed. */
function Harness() {
  const { openFormById, setValue, save, saveState, loading, error } = useIbv()
  useEffect(() => {
    openFormById(FORM_ID)
  }, [openFormById])
  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      {/* Proves a save failure never trips the load-error state that unmounts
       *  SchemaForm in IbvFormModal (VR2-187) — this stays null throughout. */}
      <span data-testid="load-error">{error ?? ""}</span>
      <span data-testid="save-state">{saveState}</span>
      <button type="button" onClick={() => setValue(PLAN_EFFECTIVE_DATE, "13/45/2026")}>
        edit
      </button>
      <button type="button" onClick={() => void save()}>
        save
      </button>
    </div>
  )
}

describe("save() failure surfaces as a toast, not the load-error banner (VR2-187)", () => {
  beforeEach(() => {
    toastError.mockReset()
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
  })

  it("shows the backend's field-specific message in a toast and re-enables Save", async () => {
    const user = userEvent.setup({ delay: null })
    mockedResolve.mockRejectedValue(
      new ApiError(
        422,
        "VALIDATION_ERROR",
        "Plan Effective Date: must be in the format M/D/YYYY",
        { fields: [PLAN_EFFECTIVE_DATE] },
      ),
    )

    render(
      <IbvProvider>
        <Harness />
      </IbvProvider>,
    )
    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"))

    await user.click(screen.getByRole("button", { name: "edit" }))
    await user.click(screen.getByRole("button", { name: "save" }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "Plan Effective Date: must be in the format M/D/YYYY",
      ),
    )
    // Save is clickable again (saveState left "saving"), and the form itself
    // never went into the load-error state a validation failure shouldn't trigger.
    expect(screen.getByTestId("save-state")).toHaveTextContent("idle")
    expect(screen.getByTestId("load-error")).toHaveTextContent("")
  })

  it("a rapid double click sends only one resolve request", async () => {
    let settle: (() => void) | undefined
    mockedResolve.mockImplementation(
      () => new Promise((resolve) => { settle = () => resolve(DETAIL) }),
    )

    render(
      <IbvProvider>
        <Harness />
      </IbvProvider>,
    )
    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"))
    fireEvent.click(screen.getByRole("button", { name: "edit" }))

    const saveButton = screen.getByRole("button", { name: "save" })
    // Two clicks with no await between them: the race a fast double click (or the
    // click events landing before React's "saving" state re-render disables the
    // button) produces. Without the savingRef guard both would call resolveDisputes.
    fireEvent.click(saveButton)
    fireEvent.click(saveButton)

    await waitFor(() => expect(screen.getByTestId("save-state")).toHaveTextContent("saving"))
    expect(mockedResolve).toHaveBeenCalledTimes(1)

    settle?.()
    await waitFor(() => expect(screen.getByTestId("save-state")).toHaveTextContent("saved"))
  })
})
