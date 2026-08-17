import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react"

import { toast } from "sonner"

import {
  isoToDateFormat,
  missingCreateLeaves,
  validateAll,
  validateCreate,
  type ValidationErrors,
} from "@/lib/ibv/validation"
import {
  allLeaves,
  createRequiredPaths,
  isApplicable,
  isRequired,
  isSatisfied,
  leafByPath,
  parseSchema,
} from "@/lib/ibv/schema"
import {
  activeDisputeValue,
  applyFlagsForPaths,
  defaultFlags,
  toggleApplied,
  toggleSwapped,
  type Dispute,
  type DisputeFlagMap,
  type DisputeFlags,
  type DisputeMap,
} from "@/lib/ibv/disputes"
import { canApplyLiveAnswer } from "@/lib/ibv/liveAnswers"
import type { FormSchema, FormValues, LeafField } from "@/lib/ibv/types"
import type { LiveDispute } from "@/lib/api/callEvents"
import { ApiError, apiErrorFieldPaths, randomId } from "@/lib/api/client"
import {
  createPatientForm,
  getPatientForm,
  getSchemaVersion,
  listInsuranceProviders,
  resolveDisputes,
  updatePatientFormStatus,
} from "@/lib/patient-forms/api"
import { applicableValues, valuesToIntakePayload } from "@/lib/patient-forms/intake"
import type {
  FieldProvenance,
  IntakeSchemaOption,
  PatientFormDetail,
  PatientFormStatus,
  ProviderOption,
} from "@/lib/patient-forms/types"
import { valueToInput } from "@/lib/patient-forms/display"
import { matchProvider } from "@/lib/patient-forms/providers"

type SaveState = "idle" | "saving" | "saved"
type Mode = "mock" | "api" | "create"

type IbvContextValue = {
  /** The form-schema document the open form is pinned to (fetched by its
   *  schema_version_id; the demo form uses the bundled dev fixture). Null while
   *  no form is open or the schema is still loading. */
  schema: FormSchema | null
  values: FormValues
  setValue: (path: string, value: string) => void
  /** Apply an AI-extracted answer pushed live over SSE (Live Monitoring). Updates
   *  the value WITHOUT marking the form dirty, and skips any field the supervisor
   *  has edited this session so an agent fill never clobbers a manual correction.
   *  `dispute` is tri-state: `undefined` leaves the disputes map untouched, `null`
   *  clears any dispute for the field, an object sets it (rendered like a REST one).
   *  `expectedFormId` is the form the SSE stream belongs to — a mismatch (or no open
   *  form) is a no-op, so a stale stream can never write one patient's value into
   *  another's form. */
  applyLiveAnswer: (
    expectedFormId: string,
    path: string,
    value: string | number | boolean | null,
    dispute?: LiveDispute | null,
  ) => void
  errors: ValidationErrors
  /** Required fields the reviewer emptied this session — saving is blocked on these. */
  clearedRequired: string[]
  disputes: DisputeMap
  disputeFor: (path: string) => Dispute | undefined
  flagsFor: (path: string) => DisputeFlags
  applyDispute: (path: string) => void
  swapDispute: (path: string) => void
  resolveAll: () => void
  /** Resolve only the disputes on the given paths — a section header passes its own leaves. */
  resolveOpenDisputes: (paths: string[]) => void
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
  /** Active insurance providers for the send-to-queue picker (empty for the
   *  demo/mock form or if the catalog fails to load). */
  providers: ProviderOption[]
  /** Picked provider id (""=none): auto-matched from the form's insurance_provider
   *  string, overridable. Sent only alongside an in_queue change to canonicalize
   *  the form's provider so dispatch resolves the right playbook. */
  providerId: string
  setProviderId: (id: string) => void
  /** A rejected status change (e.g. open disputes block completion) — shown inline. */
  statusError: string | null
  statusChanging: boolean
  /** The open form's insurance type (e.g. "infertility_treatment"); null for the
   *  demo/mock form. Format for display with `humanizeSegment`. */
  insuranceType: string | null
  /** Increments after each successful save — worklists watch it to refetch. */
  savedTick: number
  modalOpen: boolean
  /** The currently open form's id (null for mock/demo). */
  formId: string | null
  /** Returns the provenance record for a field path, or null if absent. */
  provenanceFor: (path: string) => FieldProvenance | null
  /** Open a real patient form by id, loaded (always refetched) from the API. */
  openFormById: (formId: string) => void
  /** Open the modal over the form already loaded — no refetch, no state reset. For a
   *  surface that already rendered this form inline and is expanding it. */
  openLoadedForm: () => void
  /** Load a form's data by id WITHOUT opening the full-screen modal — for surfaces
   *  (Live Monitoring) that render the form inline and apply live answers. */
  loadFormById: (formId: string) => void
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
  /** Submit the create form. Resolves to the first field path still blocking the
   *  submit (client- or server-rejected) so the caller can scroll to it, or null
   *  once the form is created. */
  submitCreate: () => Promise<string | null>
  /** Whether the field at `path` is required in the CURRENT mode: `system_fields`
   *  in create mode, the leaf's own (possibly conditional) `required` elsewhere. */
  isPathRequired: (path: string, field: LeafField) => boolean
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

/** Up to three offending field titles, so the banner stays one readable line. */
function describeBlockedSubmit(titles: string[]): string {
  if (titles.length === 0) return "Fix the highlighted fields before submitting."
  const shown = titles.slice(0, 3).join(", ")
  const rest = titles.length - 3
  return rest > 0
    ? `Fill the required fields before submitting: ${shown}, and ${rest} more.`
    : `Fill the required fields before submitting: ${shown}.`
}

/** The earliest of `paths` in schema document order — the one to scroll to. */
function firstInDocumentOrder(schema: FormSchema, paths: string[]): string | null {
  const wanted = new Set(paths)
  return allLeaves(schema).find((leaf) => wanted.has(leaf.path))?.path ?? null
}

/** `map` without `path`, same identity when the key is already absent. */
function omitPath<T>(map: Record<string, T>, path: string): Record<string, T> {
  if (!(path in map)) return map
  const next = { ...map }
  delete next[path]
  return next
}

/** Date leaves store ISO but render in the schema's declared date_format. Map each
 *  date leaf's path to that format — built on form load (adaptDetail) and memoized for
 *  the live-answer path, whose values arrive as ISO too. */
function dateFormatsOf(schema: FormSchema): Map<string, string> {
  const formats = new Map<string, string>()
  for (const leaf of allLeaves(schema)) {
    const format = leaf.field.validation?.date_format
    if (leaf.field.type === "date" && format) formats.set(leaf.path, format)
  }
  return formats
}

/** Stored value → display string, applying the date leaf's format when one is set. */
function toDisplayValue(raw: unknown, format: string | undefined): string {
  const text = valueToInput(raw)
  return format ? isoToDateFormat(text, format) : text
}

/** Map an API form detail into the form's value, dispute, and provenance maps
 * (keyed by path). Date leaves are converted from stored ISO to the schema's
 * declared date_format on the way in — see isoToDateFormat. */
function adaptDetail(
  detail: PatientFormDetail,
  dateFormats: Map<string, string>
): {
  values: FormValues
  disputes: DisputeMap
  provenance: Record<string, FieldProvenance>
} {
  const toInput = (raw: unknown, path: string): string =>
    toDisplayValue(raw, dateFormats.get(path))
  const values: FormValues = {}
  const disputes: DisputeMap = {}
  const provenance: Record<string, FieldProvenance> = {}
  for (const f of detail.fields) {
    values[f.field_path] = toInput(f.value, f.field_path)
    if (f.dispute) {
      disputes[f.field_path] = {
        previousValue: toInput(f.dispute.previous_value, f.field_path),
        currentValue: toInput(f.dispute.current_value, f.field_path),
        confidence: f.dispute.confidence ?? undefined,
        evidence: f.dispute.evidence ?? undefined,
        reasoning: f.dispute.reasoning ?? undefined,
      }
    }
    if (f.provenance) provenance[f.field_path] = f.provenance
  }
  return { values, disputes, provenance }
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
  const [provenance, setProvenance] = useState<Record<string, FieldProvenance>>({})

  // Field paths the supervisor edited this session — live AI answers skip these so
  // a push never clobbers a manual correction. A ref (not state): reading it must
  // not re-run the live-answer callback, and it's cleared whenever the form reseeds.
  const editedPathsRef = useRef<Set<string>>(new Set())

  const [dirty, setDirty] = useState(false)
  const [saveState, setSaveState] = useState<SaveState>("idle")
  // Reentrancy guard for save(): `saveState` is React state, so a rapid double
  // click can fire a second save() before the "saving" state has reached the DOM
  // and disabled the button — a ref reads/writes synchronously and closes that gap.
  const savingRef = useRef(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [savedTick, setSavedTick] = useState(0)
  const [status, setStatus] = useState<PatientFormStatus | null>(null)
  const [statusError, setStatusError] = useState<string | null>(null)
  const [statusChanging, setStatusChanging] = useState(false)
  const [insuranceType, setInsuranceType] = useState<string | null>(null)
  const [ivrNavigation, setIvrNavigation] = useState(false)
  const [providers, setProviders] = useState<ProviderOption[]>([])
  const [providerId, setProviderId] = useState<string>("")
  const [createModalOpen, setCreateModalOpen] = useState(false)
  const [createSelection, setCreateSelection] = useState<IntakeSchemaOption | null>(null)
  const [createAttempted, setCreateAttempted] = useState(false)
  const [createSubmitting, setCreateSubmitting] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  // Paths the backend itself rejected (its 422 `data.fields`), so the renderer
  // outlines them even when the client's own rules considered them fine.
  const [createServerErrors, setCreateServerErrors] = useState<ValidationErrors>({})
  // Held across retries of one submit attempt; cleared on success and on any edit.
  const createIdempotencyKeyRef = useRef<string | null>(null)

  const errors: ValidationErrors = useMemo(() => {
    if (!schema) return {}
    // Create mode: requiredness comes from system_fields; the required errors
    // only show once a submit was attempted (format errors always show live).
    if (mode === "create")
      return {
        ...createServerErrors,
        ...validateCreate(schema, values, { includeRequired: createAttempted }),
      }
    return validateAll(schema, values)
  }, [schema, values, mode, createAttempted, createServerErrors])

  const isPathRequired = useCallback(
    (path: string, field: LeafField) => {
      if (!schema) return false
      // createRequiredPaths is WeakMap-cached per schema, so this stays a Set lookup.
      if (mode === "create") return createRequiredPaths(schema).has(path)
      return isRequired(schema, field, values)
    },
    [mode, schema, values],
  )

  // Date-leaf path → declared date_format. Derived from `schema` (never stored) so it
  // can't drift from it, and so the `initialSchema` mock path gets one too. Read by the
  // live-answer path, whose values arrive as ISO just like the loaded ones.
  const dateFormats = useMemo(
    () => (schema ? dateFormatsOf(schema) : new Map<string, string>()),
    [schema],
  )

  // Required/applicable fields the reviewer emptied in THIS session (had a value on
  // load, now blank). Saving is blocked on these — the reported defect is "mandatory
  // fields cleared after upload". Computed directly (not from `errors`) so it counts
  // only genuinely-cleared fields, never a field that merely arrived empty or holds
  // a format-invalid value. A field with a declared default is never "cleared".
  const clearedRequired = useMemo(() => {
    if (!schema) return []
    return allLeaves(schema)
      .filter(
        (leaf) =>
          leaf.field.default === undefined &&
          String(values[leaf.path] ?? "").trim() === "" &&
          String(originalValues[leaf.path] ?? "").trim() !== "" &&
          isApplicable(schema, leaf.gates, values) &&
          isRequired(schema, leaf.field, values) &&
          // A pair its either/or sibling answers owes nothing, so clearing a derived "$0"
          // must not lock Save with no way out but retyping a value the rep never gave.
          !isSatisfied(schema, leaf, values),
      )
      .map((leaf) => leaf.path)
  }, [schema, values, originalValues])

  const seed = useCallback(
    (vals: FormValues, disp: DisputeMap, name: string | null) => {
      setValues(vals)
      setOriginalValues(vals)
      setDisputes(disp)
      setFlagsState({})
      setDirty(false)
      setSaveState("idle")
      setPatientName(name)
      editedPathsRef.current = new Set() // fresh form/save — no manual edits yet
    },
    [],
  )

  // Real path: load a patient form by id from the API into the form state (schema +
  // values + provenance) WITHOUT opening the full-screen form modal. The Live
  // Monitoring modal uses this to render its inline form and receive live answers;
  // openFormById layers the full-screen modal on top. setState happens in the event
  // handler + async callbacks, never synchronously inside an effect.
  const loadFormById = useCallback(
    (id: string) => {
      setMode("api")
      setFormId(id)
      setLoading(true)
      setError(null)
      setValues({})
      setOriginalValues({})
      setDisputes({})
      setFlagsState({})
      editedPathsRef.current = new Set()
      setDirty(false)
      setSaveState("idle")
      setStatus(null)
      setStatusError(null)
      setInsuranceType(null)
      setProvenance({})
      setIvrNavigation(false)
      setProviders([])
      setProviderId("")
      setSchema(null)
      getPatientForm(id)
        .then(async (detail) => {
          // Render against the exact document the form is pinned to — never a
          // bundled copy (schema_version_id is the contract). Load the provider
          // catalog alongside — a failed load is non-fatal (picker stays empty).
          const [loaded, providerList] = await Promise.all([
            loadSchema(detail.schema_version_id),
            listInsuranceProviders().catch(() => [] as ProviderOption[]),
          ])
          // `loaded`, not the dateFormats memo — that reads `schema`, which is set below.
          const { values: v, disputes: d, provenance: prov } = adaptDetail(
            detail,
            dateFormatsOf(loaded),
          )
          // seed() replaces `values` wholesale, so a live answer that landed during this
          // fetch is dropped. Self-healing — the SSE replays from "0" on connect and the
          // value is already persisted — so the field is at worst stale until reload.
          seed(v, d, detail.patient_name)
          setSchema(loaded)
          setStatus(detail.status)
          setInsuranceType(detail.insurance_type)
          setProvenance(prov)
          setIvrNavigation(detail.ivr_navigation_enabled)
          setProviders(providerList)
          setProviderId(matchProvider(providerList, detail.insurance_provider))
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

  // Always refetches: reopening a form someone else has since edited must show their changes.
  const openFormById = useCallback(
    (id: string) => {
      setModalOpen(true)
      loadFormById(id)
    },
    [loadFormById],
  )

  const openLoadedForm = useCallback(() => setModalOpen(true), [])

  const closeForm = useCallback(() => {
    setModalOpen(false)
    setProvenance({})
  }, [])

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
    // Provenance is keyed by schema-relative path, so a previously-open form's map
    // would otherwise paint its badges onto the empty create form.
    setProvenance({})
    setIvrNavigation(false)
    setProviders([])
    setProviderId("")
    setSchema(null)
    setCreateSelection(null)
    setCreateAttempted(false)
    setCreateError(null)
    setCreateServerErrors({})
    createIdempotencyKeyRef.current = null
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
    if (!schema || !createSelection) return null
    setCreateAttempted(true)
    setCreateServerErrors((prev) => (Object.keys(prev).length === 0 ? prev : {}))
    const blocking = missingCreateLeaves(schema, values)
    if (blocking.length > 0) {
      setCreateError(describeBlockedSubmit(blocking.map((leaf) => leaf.field.title)))
      return blocking[0].path
    }
    // A format error on a filled field blocks too, but has no title list to name.
    const invalid = Object.keys(validateCreate(schema, values))
    if (invalid.length > 0) {
      setCreateError("Fix the highlighted fields before submitting.")
      return firstInDocumentOrder(schema, invalid)
    }
    setCreateError(null)
    setCreateSubmitting(true)
    // One key per payload, reused while THIS attempt is retried (setValue clears it),
    // so a retry after an ambiguous failure cannot land a second form.
    createIdempotencyKeyRef.current ??= randomId()
    try {
      await createPatientForm(
        createSelection.schema_id,
        createSelection.published_version_id,
        valuesToIntakePayload(applicableValues(schema, values)),
        createIdempotencyKeyRef.current,
      )
      createIdempotencyKeyRef.current = null
      setCreateModalOpen(false)
      toast.success("Patient form created.")
      setSavedTick((t) => t + 1) // the worklist refetches so the new row shows
      return null
    } catch (err) {
      // e.g. 409 "no published version" / "a newer version has been published"
      // (demoted or promoted mid-flow), or the backend's authoritative 422.
      setCreateError(
        err instanceof ApiError ? err.message : "Could not create the patient form.",
      )
      const byPath = leafByPath(schema)
      const rejected = apiErrorFieldPaths(err).filter((p) => byPath.has(p))
      if (rejected.length === 0) return null
      setCreateServerErrors(
        Object.fromEntries(rejected.map((p) => [p, "Rejected by the server."])),
      )
      return firstInDocumentOrder(schema, rejected)
    } finally {
      setCreateSubmitting(false)
    }
  }, [schema, createSelection, values])

  const setValue = useCallback((path: string, value: string) => {
    editedPathsRef.current.add(path) // a manual edit — live AI answers must not overwrite it
    setValues((prev) => ({ ...prev, [path]: value }))
    setDirty(true)
    setSaveState("idle")
    // The server's verdict on this path is stale the moment the user retypes it,
    // and a fresh payload deserves a fresh idempotency key.
    setCreateServerErrors((prev) => omitPath(prev, path))
    createIdempotencyKeyRef.current = null
  }, [])

  const applyLiveAnswer = useCallback(
    (
      expectedFormId: string,
      path: string,
      raw: string | number | boolean | null,
      dispute?: LiveDispute | null,
    ) => {
      const applicable = canApplyLiveAnswer({
        loadedFormId: formId,
        expectedFormId,
        path,
        editedPaths: editedPathsRef.current,
      })
      if (!applicable) return
      const format = dateFormats.get(path)
      const display = toDisplayValue(raw, format)
      // Not a supervisor edit: update the value only, never touch dirty/saveState.
      setValues((prev) => (prev[path] === display ? prev : { ...prev, [path]: display }))

      if (dispute === undefined) return // frame carried no dispute info — leave disputes as-is
      if (dispute === null) {
        // Backend computed "not disputed" — drop any dispute (and its flag) for the field.
        setDisputes((prev) => omitPath(prev, path))
        setFlagsState((prev) => omitPath(prev, path))
        return
      }
      // Set the dispute in the same shape adaptDetail builds, so FieldRow renders it
      // identically to a REST-loaded one. Flags stay unset (starts unresolved); no dirty.
      setDisputes((prev) => ({
        ...prev,
        [path]: {
          previousValue: toDisplayValue(dispute.previousValue, format),
          currentValue: toDisplayValue(dispute.currentValue, format),
          confidence: dispute.confidence ?? undefined,
          evidence: dispute.evidence ?? undefined,
          reasoning: dispute.reasoning ?? undefined,
        },
      }))
    },
    [formId, dateFormats],
  )

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

  // Marks disputes applied WITHOUT touching values — like the per-field ✓ — so a
  // manual correction made before resolving is saved, never reverted to the
  // captured value (a disputed field already holds that value unless edited).
  const resolveOpenDisputes = useCallback(
    (paths: string[]) => {
      setFlagsState((prev) => applyFlagsForPaths(disputes, prev, paths))
      setDirty(true)
      setSaveState("idle")
    },
    [disputes],
  )

  const resolveAll = useCallback(
    () => resolveOpenDisputes(Object.keys(disputes)),
    [disputes, resolveOpenDisputes],
  )

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
          next === "in_queue"
            ? {
                enableIvrNavigation: ivrNavigation,
                insuranceProviderId: providerId || undefined,
              }
            : undefined,
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
    [mode, formId, ivrNavigation, providerId],
  )

  const save = useCallback(async () => {
    // A rapid double click (or a second call before "saving" reaches the DOM and
    // disables the button) must not fire a second overlapping request — each one
    // would independently 422 and toast the same error. savingRef is synchronous,
    // unlike saveState, so it closes the gap React's own re-render can't.
    if (savingRef.current) return
    savingRef.current = true
    try {
      // Block save when the reviewer cleared a mandatory field that had a value on
      // load. The Save button is also disabled in this state — this guards any
      // programmatic call. Fields that arrived empty don't block (partial save OK).
      if (clearedRequired.length > 0) {
        toast.error("Restore the cleared required fields before saving.")
        setSaveState("idle")
        return
      }
      setSaveState("saving")
      if (mode === "mock" || !formId || !schema) {
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
        const { values: v, disputes: d, provenance: prov } = adaptDetail(refreshed, dateFormats)
        seed(v, d, refreshed.patient_name)
        setStatus(refreshed.status)
        setStatusError(null) // resolving disputes clears any "resolve first" warning
        setProvenance(prov)
        setSaveState("saved")
        setSavedTick((t) => t + 1)
      } catch (err) {
        // A save-time failure (e.g. a validation 422) doesn't invalidate the loaded
        // form, so it must not trip the load-error state that unmounts SchemaForm
        // (IbvFormModal renders the form only while `error` is null) — a toast keeps
        // the form (and the offending field) visible so the reviewer can fix it.
        toast.error(err instanceof ApiError ? err.message : "Could not save changes.")
        setSaveState("idle")
      }
    } finally {
      savingRef.current = false
    }
  }, [
    mode,
    formId,
    schema,
    dateFormats,
    values,
    originalValues,
    disputes,
    flags,
    seed,
    clearedRequired,
  ])

  const provenanceFor = useCallback(
    (path: string) => provenance[path] ?? null,
    [provenance],
  )

  const value: IbvContextValue = {
    schema,
    values,
    setValue,
    applyLiveAnswer,
    errors,
    clearedRequired,
    disputes,
    disputeFor,
    flagsFor,
    applyDispute,
    swapDispute,
    resolveAll,
    resolveOpenDisputes,
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
    providers,
    providerId,
    setProviderId,
    statusError,
    statusChanging,
    insuranceType,
    savedTick,
    modalOpen,
    formId,
    provenanceFor,
    openFormById,
    openLoadedForm,
    loadFormById,
    closeForm,
    createModalOpen,
    openCreate,
    closeCreate,
    beginCreate,
    createSelection,
    createSubmitting,
    createError,
    submitCreate,
    isPathRequired,
  }

  return <IbvContext.Provider value={value}>{children}</IbvContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useIbv() {
  const ctx = useContext(IbvContext)
  if (!ctx) throw new Error("useIbv must be used within <IbvProvider>")
  return ctx
}
