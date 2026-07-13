import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react"

import { validateAll, validateCreate, type ValidationErrors } from "@/lib/ibv/validation"
import { allLeaves, parseSchema } from "@/lib/ibv/schema"
import { demoSchema, mockValues } from "@/lib/ibv/mock"
import {
  activeDisputeValue,
  applyAllFlags,
  defaultFlags,
  mockDisputes,
  seedValues,
  toggleApplied,
  toggleSwapped,
  type Dispute,
  type DisputeFlagMap,
  type DisputeFlags,
  type DisputeMap,
} from "@/lib/ibv/disputes"
import type { FormSchema, FormValues } from "@/lib/ibv/types"
import { ApiError } from "@/lib/api/client"
import {
  createPatientForm,
  getPatientForm,
  getSchemaVersion,
  resolveDisputes,
  updatePatientFormStatus,
} from "@/lib/patient-forms/api"
import { valuesToIntakePayload } from "@/lib/patient-forms/intake"
import type {
  IntakeSchemaOption,
  PatientFormDetail,
  PatientFormStatus,
} from "@/lib/patient-forms/types"
import { valueToInput } from "@/lib/patient-forms/display"

type SaveState = "idle" | "saving" | "saved"
type Mode = "mock" | "api" | "create"

type IbvContextValue = {
  /** The form-schema document the open form is pinned to (fetched by its
   *  schema_version_id; the demo form uses the bundled dev fixture). Null while
   *  no form is open or the schema is still loading. */
  schema: FormSchema | null
  values: FormValues
  setValue: (path: string, value: string) => void
  errors: ValidationErrors
  disputes: DisputeMap
  disputeFor: (path: string) => Dispute | undefined
  flagsFor: (path: string) => DisputeFlags
  applyDispute: (path: string) => void
  swapDispute: (path: string) => void
  resolveAll: () => void
  pendingDisputeCount: number
  dirty: boolean
  saveState: SaveState
  save: () => Promise<void>
  loading: boolean
  error: string | null
  patientName: string | null
  /** Current lifecycle status of the open form (null for the demo/mock form). */
  status: PatientFormStatus | null
  /** Change the form's status via the dedicated endpoint (status-only). */
  changeStatus: (next: PatientFormStatus) => Promise<void>
  /** Voice-lab-style opt-out: run the IVR navigator on this form's calls.
   *  Pre-loaded from the form detail; sent only alongside an in_queue change. */
  ivrNavigation: boolean
  setIvrNavigation: (v: boolean) => void
  /** A rejected status change (e.g. open disputes block completion) — shown inline. */
  statusError: string | null
  statusChanging: boolean
  /** The open form's insurance type (e.g. "infertility_treatment"); null for the
   *  demo/mock form. Format for display with `humanizeSegment`. */
  insuranceType: string | null
  /** Increments after each successful save — worklists watch it to refetch. */
  savedTick: number
  modalOpen: boolean
  /** Open the form with demo data (Live Monitoring). */
  openForm: () => void
  /** Open a real patient form by id, loaded from the API. */
  openFormById: (formId: string) => void
  closeForm: () => void
  /** In-app create flow (Data Management → Add patient form). */
  createModalOpen: boolean
  /** Open the create modal at the schema-picker step (also the Back action). */
  openCreate: () => void
  closeCreate: () => void
  /** Bind the picked family: load its published schema, seed leaf defaults. */
  beginCreate: (option: IntakeSchemaOption) => Promise<void>
  /** The picked family, or null while still on the picker step. */
  createSelection: IntakeSchemaOption | null
  createSubmitting: boolean
  /** Modal-level create failure (stale published version, network) — a banner. */
  createError: string | null
  submitCreate: () => Promise<void>
}

const IbvContext = createContext<IbvContextValue | null>(null)

// schema_version rows are immutable, so fetched documents cache for the session.
const schemaCache = new Map<string, FormSchema>()

async function loadSchema(versionId: string): Promise<FormSchema> {
  const cached = schemaCache.get(versionId)
  if (cached) return cached
  const detail = await getSchemaVersion(versionId)
  const schema = parseSchema(detail.document)
  schemaCache.set(versionId, schema)
  return schema
}

/** Map an API form detail into the form's value + dispute maps (keyed by path). */
function adaptDetail(detail: PatientFormDetail): {
  values: FormValues
  disputes: DisputeMap
} {
  const values: FormValues = {}
  const disputes: DisputeMap = {}
  for (const f of detail.fields) {
    values[f.field_path] = valueToInput(f.value)
    if (f.dispute) {
      disputes[f.field_path] = {
        previousValue: valueToInput(f.dispute.previous_value),
        currentValue: valueToInput(f.dispute.current_value),
        confidence: f.dispute.confidence ?? undefined,
        evidence: f.dispute.evidence ?? undefined,
        reasoning: f.dispute.reasoning ?? undefined,
      }
    }
  }
  return { values, disputes }
}

export function IbvProvider({
  children,
  initialSchema = null,
}: {
  children: ReactNode
  /** Pre-seeded schema for tests/stories; real forms load theirs per form. */
  initialSchema?: FormSchema | null
}) {
  const [modalOpen, setModalOpen] = useState(false)
  const [mode, setMode] = useState<Mode>("mock")
  const [formId, setFormId] = useState<string | null>(null)
  const [patientName, setPatientName] = useState<string | null>(null)

  const [schema, setSchema] = useState<FormSchema | null>(initialSchema)
  const [values, setValues] = useState<FormValues>({})
  const [originalValues, setOriginalValues] = useState<FormValues>({})
  const [disputes, setDisputes] = useState<DisputeMap>({})
  const [flags, setFlagsState] = useState<DisputeFlagMap>({})

  const [dirty, setDirty] = useState(false)
  const [saveState, setSaveState] = useState<SaveState>("idle")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [savedTick, setSavedTick] = useState(0)
  const [status, setStatus] = useState<PatientFormStatus | null>(null)
  const [statusError, setStatusError] = useState<string | null>(null)
  const [statusChanging, setStatusChanging] = useState(false)
  const [insuranceType, setInsuranceType] = useState<string | null>(null)
  const [ivrNavigation, setIvrNavigation] = useState(true)
  const [createModalOpen, setCreateModalOpen] = useState(false)
  const [createSelection, setCreateSelection] = useState<IntakeSchemaOption | null>(null)
  const [createAttempted, setCreateAttempted] = useState(false)
  const [createSubmitting, setCreateSubmitting] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)

  const errors: ValidationErrors = useMemo(() => {
    if (!schema) return {}
    // Create mode: requiredness comes from system_fields; the required errors
    // only show once a submit was attempted (format errors always show live).
    if (mode === "create")
      return validateCreate(schema, values, { includeRequired: createAttempted })
    return validateAll(schema, values)
  }, [schema, values, mode, createAttempted])

  const seed = useCallback(
    (vals: FormValues, disp: DisputeMap, name: string | null) => {
      setValues(vals)
      setOriginalValues(vals)
      setDisputes(disp)
      setFlagsState({})
      setDirty(false)
      setSaveState("idle")
      setPatientName(name)
    },
    [],
  )

  // Demo path (Live Monitoring): seed from the bundled mock + dev-fixture schema.
  const openForm = useCallback(() => {
    setMode("mock")
    setFormId(null)
    setError(null)
    setLoading(false)
    setStatus(null)
    setStatusError(null)
    setInsuranceType(null)
    setIvrNavigation(true)
    setSchema(demoSchema)
    seed({ ...mockValues, ...seedValues(mockDisputes) }, mockDisputes, "Demo Patient")
    setModalOpen(true)
  }, [seed])

  // Real path: load a patient form by id from the API. setState happens in the
  // event handler + async callbacks, never synchronously inside an effect.
  const openFormById = useCallback(
    (id: string) => {
      setMode("api")
      setFormId(id)
      setModalOpen(true)
      setLoading(true)
      setError(null)
      setValues({})
      setOriginalValues({})
      setDisputes({})
      setFlagsState({})
      setDirty(false)
      setSaveState("idle")
      setStatus(null)
      setStatusError(null)
      setInsuranceType(null)
      setIvrNavigation(true)
      setSchema(null)
      getPatientForm(id)
        .then(async (detail) => {
          // Render against the exact document the form is pinned to — never a
          // bundled copy (schema_version_id is the contract).
          const loaded = await loadSchema(detail.schema_version_id)
          const { values: v, disputes: d } = adaptDetail(detail)
          seed(v, d, detail.patient_name)
          setSchema(loaded)
          setStatus(detail.status)
          setInsuranceType(detail.insurance_type)
          setIvrNavigation(detail.ivr_navigation_enabled)
        })
        .catch((err) => {
          // ApiError and the parseSchema dsl_version guard both carry a
          // human-readable, non-PHI message.
          setError(err instanceof Error ? err.message : "Could not load this form.")
        })
        .finally(() => setLoading(false))
    },
    [seed],
  )

  const closeForm = useCallback(() => setModalOpen(false), [])

  // Create path: step 1 (picker) has no schema; beginCreate loads the published
  // document and seeds declared defaults so what the user sees is what submits.
  const openCreate = useCallback(() => {
    setMode("create")
    setFormId(null)
    setError(null)
    setLoading(false)
    setStatus(null)
    setStatusError(null)
    setInsuranceType(null)
    setSchema(null)
    setCreateSelection(null)
    setCreateAttempted(false)
    setCreateError(null)
    seed({}, {}, null)
    setCreateModalOpen(true)
  }, [seed])

  const closeCreate = useCallback(() => setCreateModalOpen(false), [])

  const beginCreate = useCallback(
    async (option: IntakeSchemaOption) => {
      setCreateError(null)
      setLoading(true)
      try {
        const loaded = await loadSchema(option.published_version_id)
        const defaults: FormValues = {}
        for (const leaf of allLeaves(loaded)) {
          if (leaf.field.default !== undefined) defaults[leaf.path] = leaf.field.default
        }
        seed(defaults, {}, null)
        setSchema(loaded)
        setInsuranceType(option.insurance_type)
        setCreateSelection(option)
      } catch (err) {
        // ApiError and the parseSchema dsl_version guard both carry a
        // human-readable, non-PHI message.
        setCreateError(
          err instanceof Error ? err.message : "Could not load this form schema.",
        )
      } finally {
        setLoading(false)
      }
    },
    [seed],
  )

  const submitCreate = useCallback(async () => {
    if (!schema || !createSelection) return
    setCreateAttempted(true)
    if (Object.keys(validateCreate(schema, values)).length > 0) {
      setCreateError("Fill the required fields before submitting.")
      return
    }
    setCreateError(null)
    setCreateSubmitting(true)
    try {
      await createPatientForm(createSelection.schema_id, valuesToIntakePayload(values))
      setCreateModalOpen(false)
      setSavedTick((t) => t + 1) // worklist refetches; the new row is the feedback
    } catch (err) {
      // e.g. 409 "this form schema has no published version" (demoted mid-flow),
      // or the backend's authoritative 422 — surfaced as the modal banner.
      setCreateError(
        err instanceof ApiError ? err.message : "Could not create the patient form.",
      )
    } finally {
      setCreateSubmitting(false)
    }
  }, [schema, createSelection, values])

  const setValue = useCallback((path: string, value: string) => {
    setValues((prev) => ({ ...prev, [path]: value }))
    setDirty(true)
    setSaveState("idle")
  }, [])

  const flagsFor = useCallback(
    (path: string) => flags[path] ?? defaultFlags(),
    [flags],
  )
  const disputeFor = useCallback((path: string) => disputes[path], [disputes])

  const setFlags = useCallback((path: string, next: DisputeFlags) => {
    setFlagsState((prev) => ({ ...prev, [path]: next }))
    setDirty(true)
    setSaveState("idle")
  }, [])

  const applyDispute = useCallback(
    (path: string) => setFlags(path, toggleApplied(flags[path] ?? defaultFlags())),
    [flags, setFlags],
  )

  const swapDispute = useCallback(
    (path: string) => {
      const d = disputes[path]
      if (!d) return
      const next = toggleSwapped(flags[path] ?? defaultFlags())
      setFlags(path, next)
      setValue(path, activeDisputeValue(d, next))
    },
    [disputes, flags, setFlags, setValue],
  )

  const resolveAll = useCallback(() => {
    setFlagsState((prev) => applyAllFlags(disputes, prev))
    setValues((prev) => {
      const next = { ...prev }
      for (const [path, d] of Object.entries(disputes)) next[path] = d.currentValue
      return next
    })
    setDirty(true)
    setSaveState("idle")
  }, [disputes])

  const pendingDisputeCount = useMemo(
    () => Object.keys(disputes).filter((p) => !(flags[p]?.applied ?? false)).length,
    [disputes, flags],
  )

  const changeStatus = useCallback(
    async (next: PatientFormStatus) => {
      setStatusError(null)
      // Demo/mock form has no backend row — reflect the change locally only.
      if (mode === "mock" || !formId) {
        setStatus(next)
        return
      }
      setStatusChanging(true)
      try {
        const res = await updatePatientFormStatus(
          formId,
          next,
          next === "in_queue" ? { enableIvrNavigation: ivrNavigation } : undefined,
        )
        setStatus(res.status)
        setSavedTick((t) => t + 1) // worklist refetches the new status
      } catch (err) {
        // e.g. 409 "resolve all disputes before completing this form" — warn inline.
        setStatusError(
          err instanceof ApiError ? err.message : "Could not change the status.",
        )
      } finally {
        setStatusChanging(false)
      }
    },
    [mode, formId, ivrNavigation],
  )

  const save = useCallback(async () => {
    setSaveState("saving")
    if (mode === "mock" || !formId) {
      await new Promise((r) => setTimeout(r, 400))
      setDirty(false)
      setSaveState("saved")
      setSavedTick((t) => t + 1)
      return
    }
    // API: edited values are corrections; applied-but-unchanged disputes are accepts.
    const form_data: Record<string, string> = {}
    const dispute_fields: string[] = []
    const paths = new Set([...Object.keys(values), ...Object.keys(disputes)])
    for (const path of paths) {
      const changed = values[path] !== originalValues[path]
      if (changed) {
        form_data[path] = values[path] ?? ""
      } else if (disputes[path] && flags[path]?.applied) {
        form_data[path] = values[path] ?? ""
        dispute_fields.push(path)
      }
    }
    try {
      const refreshed = await resolveDisputes(formId, {
        form_data,
        dispute_fields,
        reasked_fields: [],
      })
      const { values: v, disputes: d } = adaptDetail(refreshed)
      seed(v, d, refreshed.patient_name)
      setStatus(refreshed.status)
      setStatusError(null) // resolving disputes clears any "resolve first" warning
      setSaveState("saved")
      setSavedTick((t) => t + 1)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save changes.")
      setSaveState("idle")
    }
  }, [mode, formId, values, originalValues, disputes, flags, seed])

  const value: IbvContextValue = {
    schema,
    values,
    setValue,
    errors,
    disputes,
    disputeFor,
    flagsFor,
    applyDispute,
    swapDispute,
    resolveAll,
    pendingDisputeCount,
    dirty,
    saveState,
    save,
    loading,
    error,
    patientName,
    status,
    changeStatus,
    ivrNavigation,
    setIvrNavigation,
    statusError,
    statusChanging,
    insuranceType,
    savedTick,
    modalOpen,
    openForm,
    openFormById,
    closeForm,
    createModalOpen,
    openCreate,
    closeCreate,
    beginCreate,
    createSelection,
    createSubmitting,
    createError,
    submitCreate,
  }

  return <IbvContext.Provider value={value}>{children}</IbvContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useIbv() {
  const ctx = useContext(IbvContext)
  if (!ctx) throw new Error("useIbv must be used within <IbvProvider>")
  return ctx
}
