import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react"

import { validateAll, type ValidationErrors } from "@/lib/ibv/validation"
import { mockValues } from "@/lib/ibv/mock"
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
import type { FormValues } from "@/lib/ibv/types"
import { ApiError } from "@/lib/api/client"
import {
  getPatientForm,
  resolveDisputes,
  updatePatientFormStatus,
} from "@/lib/patient-forms/api"
import type { PatientFormDetail, PatientFormStatus } from "@/lib/patient-forms/types"
import { valueToInput } from "@/lib/patient-forms/display"

type SaveState = "idle" | "saving" | "saved"
type Mode = "mock" | "api"

type IbvContextValue = {
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
  /** A rejected status change (e.g. open disputes block completion) — shown inline. */
  statusError: string | null
  statusChanging: boolean
  /** Increments after each successful save — worklists watch it to refetch. */
  savedTick: number
  modalOpen: boolean
  /** Open the form with demo data (Live Monitoring). */
  openForm: () => void
  /** Open a real patient form by id, loaded from the API. */
  openFormById: (formId: string) => void
  closeForm: () => void
}

const IbvContext = createContext<IbvContextValue | null>(null)

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

export function IbvProvider({ children }: { children: ReactNode }) {
  const [modalOpen, setModalOpen] = useState(false)
  const [mode, setMode] = useState<Mode>("mock")
  const [formId, setFormId] = useState<string | null>(null)
  const [patientName, setPatientName] = useState<string | null>(null)

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

  const errors = useMemo(() => validateAll(values), [values])

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

  // Demo path (Live Monitoring): seed from the bundled mock.
  const openForm = useCallback(() => {
    setMode("mock")
    setFormId(null)
    setError(null)
    setLoading(false)
    setStatus(null)
    setStatusError(null)
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
      getPatientForm(id)
        .then((detail) => {
          const { values: v, disputes: d } = adaptDetail(detail)
          seed(v, d, detail.patient_name)
          setStatus(detail.status)
        })
        .catch((err) => {
          setError(err instanceof ApiError ? err.message : "Could not load this form.")
        })
        .finally(() => setLoading(false))
    },
    [seed],
  )

  const closeForm = useCallback(() => setModalOpen(false), [])

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
        const res = await updatePatientFormStatus(formId, next)
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
    [mode, formId],
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
    statusError,
    statusChanging,
    savedTick,
    modalOpen,
    openForm,
    openFormById,
    closeForm,
  }

  return <IbvContext.Provider value={value}>{children}</IbvContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useIbv() {
  const ctx = useContext(IbvContext)
  if (!ctx) throw new Error("useIbv must be used within <IbvProvider>")
  return ctx
}
