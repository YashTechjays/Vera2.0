import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react"

import { completionPercent } from "@/lib/ibv/schema"
import { validateAll, type ValidationErrors } from "@/lib/ibv/validation"
import {
  mockPeople,
  mockValues,
  disputesByPerson,
  saveIbvForms,
} from "@/lib/ibv/mock"
import {
  activeDisputeValue,
  applyAllFlags,
  buildSavePayload,
  defaultFlags,
  seedValues,
  toggleApplied,
  toggleSwapped,
  type Dispute,
  type DisputeFlagMap,
  type DisputeFlags,
  type DisputeMap,
} from "@/lib/ibv/disputes"
import type { FormValues, InsuredPerson } from "@/lib/ibv/types"

type SaveState = "idle" | "saving" | "saved"
export type FormStatus = "Not started" | "In progress" | "Complete"

type IbvContextValue = {
  people: InsuredPerson[]
  completionById: Record<string, number>
  statusById: Record<string, FormStatus>
  // active form
  activeId: string
  setActiveId: (id: string) => void
  values: FormValues
  setValue: (path: string, value: string) => void
  errors: ValidationErrors
  dirty: boolean
  saveState: SaveState
  save: () => Promise<void>
  // disputes (active person)
  disputes: DisputeMap
  disputeFor: (path: string) => Dispute | undefined
  flagsFor: (path: string) => DisputeFlags
  applyDispute: (path: string) => void
  swapDispute: (path: string) => void
  resolveAll: () => void
  pendingDisputeCount: number
  // modal control
  modalOpen: boolean
  openForm: (personId?: string) => void
  closeForm: () => void
}

const IbvContext = createContext<IbvContextValue | null>(null)

function statusOf(pct: number): FormStatus {
  if (pct === 0) return "Not started"
  if (pct === 100) return "Complete"
  return "In progress"
}

export function IbvProvider({ children }: { children: ReactNode }) {
  const people = mockPeople
  const [activeId, setActiveId] = useState(people[0]?.id ?? "")

  const [valuesByPerson, setValuesByPerson] = useState<
    Record<string, FormValues>
  >(() =>
    Object.fromEntries(
      people.map((p) => [
        p.id,
        { ...mockValues, ...seedValues(disputesByPerson[p.id] ?? {}) },
      ])
    )
  )
  const [flagsByPerson, setFlagsByPerson] = useState<
    Record<string, DisputeFlagMap>
  >(() => Object.fromEntries(people.map((p) => [p.id, {}])))
  const [dirty, setDirty] = useState(false)
  const [saveState, setSaveState] = useState<SaveState>("idle")
  const [modalOpen, setModalOpen] = useState(false)

  // Stable identities so the dependent useMemos below don't recompute every
  // render (persons without seeded disputes/flags would otherwise get a fresh
  // `{}` each time).
  const values = useMemo(
    () => valuesByPerson[activeId] ?? {},
    [valuesByPerson, activeId]
  )
  const disputes = useMemo(
    () => disputesByPerson[activeId] ?? {},
    [activeId]
  )
  const flags = useMemo(() => flagsByPerson[activeId] ?? {}, [flagsByPerson, activeId])

  const errors = useMemo(() => validateAll(values), [values])

  const setValue = useCallback(
    (path: string, value: string) => {
      setValuesByPerson((prev) => ({
        ...prev,
        [activeId]: { ...prev[activeId], [path]: value },
      }))
      setDirty(true)
      setSaveState("idle")
    },
    [activeId]
  )

  const flagsFor = useCallback(
    (path: string) => flagsByPerson[activeId]?.[path] ?? defaultFlags(),
    [activeId, flagsByPerson]
  )

  const setFlags = useCallback(
    (path: string, next: DisputeFlags) => {
      setFlagsByPerson((prev) => ({
        ...prev,
        [activeId]: { ...prev[activeId], [path]: next },
      }))
      setDirty(true)
      setSaveState("idle")
    },
    [activeId]
  )

  const applyDispute = useCallback(
    (path: string) => setFlags(path, toggleApplied(flagsFor(path))),
    [flagsFor, setFlags]
  )

  const swapDispute = useCallback(
    (path: string) => {
      const d = disputesByPerson[activeId]?.[path]
      if (!d) return
      const next = toggleSwapped(flagsFor(path))
      setFlags(path, next)
      setValue(path, activeDisputeValue(d, next))
    },
    [activeId, flagsFor, setFlags, setValue]
  )

  const resolveAll = useCallback(() => {
    const personDisputes = disputesByPerson[activeId] ?? {}
    setFlagsByPerson((prev) => ({
      ...prev,
      [activeId]: applyAllFlags(personDisputes, prev[activeId] ?? {}),
    }))
    setValuesByPerson((prev) => {
      const nextValues = { ...prev[activeId] }
      for (const [path, d] of Object.entries(personDisputes)) {
        nextValues[path] = d.currentValue
      }
      return { ...prev, [activeId]: nextValues }
    })
    setDirty(true)
    setSaveState("idle")
  }, [activeId])

  const disputeFor = useCallback(
    (path: string) => disputesByPerson[activeId]?.[path],
    [activeId]
  )

  const pendingDisputeCount = useMemo(
    () =>
      Object.keys(disputes).filter((p) => !(flags[p]?.applied ?? false)).length,
    [disputes, flags]
  )

  const completionById = useMemo(
    () =>
      Object.fromEntries(
        people.map((p) => [p.id, completionPercent(valuesByPerson[p.id] ?? {})])
      ),
    [people, valuesByPerson]
  )

  const statusById = useMemo(
    () =>
      Object.fromEntries(
        people.map((p) => [p.id, statusOf(completionById[p.id] ?? 0)])
      ) as Record<string, FormStatus>,
    [people, completionById]
  )

  const save = useCallback(async () => {
    setSaveState("saving")
    const payload = Object.fromEntries(
      people.map((p) => [
        p.id,
        buildSavePayload(
          valuesByPerson[p.id] ?? {},
          disputesByPerson[p.id] ?? {},
          flagsByPerson[p.id] ?? {}
        ),
      ])
    )
    await saveIbvForms(payload)
    setDirty(false)
    setSaveState("saved")
  }, [people, valuesByPerson, flagsByPerson])

  const openForm = useCallback((personId?: string) => {
    if (personId) setActiveId(personId)
    setModalOpen(true)
  }, [])
  const closeForm = useCallback(() => setModalOpen(false), [])

  const value: IbvContextValue = {
    people,
    completionById,
    statusById,
    activeId,
    setActiveId,
    values,
    setValue,
    errors,
    dirty,
    saveState,
    save,
    disputes,
    disputeFor,
    flagsFor,
    applyDispute,
    swapDispute,
    resolveAll,
    pendingDisputeCount,
    modalOpen,
    openForm,
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
