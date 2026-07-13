import { useEffect, useState } from "react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Select } from "@/components/ui/select"
import { listIntakeSchemas } from "@/lib/patient-forms/api"
import type { IntakeSchemaOption } from "@/lib/patient-forms/types"
import { humanizeSegment } from "@/lib/patient-forms/display"
import { useIbv } from "./IbvProvider"
import { SchemaForm } from "./SchemaForm"

/** The schema-picker step, split out so its local state (options, the
 *  in-progress selection) resets by mounting fresh rather than by an effect
 *  reacting to a boolean — it mounts each time the dialog opens and each
 *  time "Back" returns from the form step (Radix unmounts `DialogContent`'s
 *  children on close, and the parent's step ternary unmounts this branch
 *  when swapping to the form step). */
function SchemaPicker({
  createError,
  loading,
  onContinue,
  onCancel,
}: {
  createError: string | null
  loading: boolean
  onContinue: (option: IntakeSchemaOption) => void
  onCancel: () => void
}) {
  const [options, setOptions] = useState<IntakeSchemaOption[] | null>(null)
  const [optionsError, setOptionsError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState("")

  // Fresh catalog read on mount — cheap read, and it avoids offering a family
  // whose version was demoted since the dialog was last opened.
  useEffect(() => {
    let cancelled = false
    listIntakeSchemas()
      .then((res) => {
        if (!cancelled) setOptions(res)
      })
      .catch((err) => {
        if (!cancelled)
          setOptionsError(
            err instanceof Error ? err.message : "Could not load form schemas.",
          )
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="space-y-4 p-4">
      {optionsError && (
        <p className="text-sm text-destructive" role="alert">
          {optionsError}
        </p>
      )}
      {options === null && !optionsError && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}
      {options?.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No published form schemas are available yet.
        </p>
      )}
      {options && options.length > 0 && (
        <Select value={selectedId} onChange={(e) => setSelectedId(e.target.value)}>
          <option value="">Select a form schema…</option>
          {options.map((o) => (
            <option key={o.schema_id} value={o.schema_id}>
              {o.name} ({humanizeSegment(o.insurance_type)})
            </option>
          ))}
        </Select>
      )}
      {createError && (
        <p className="text-sm text-destructive" role="alert">
          {createError}
        </p>
      )}
      <div className="flex justify-end gap-3">
        <Button variant="outline" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          disabled={!selectedId || loading}
          onClick={() => {
            const option = options?.find((o) => o.schema_id === selectedId)
            if (option) onContinue(option)
          }}
        >
          {loading ? "Loading…" : "Continue"}
        </Button>
      </div>
    </div>
  )
}

/** Two-step create flow: pick a form family (only families with a published
 *  version are offered), then fill its published schema in the same renderer
 *  the review modal uses. Values are held in IbvProvider create mode. */
export function CreatePatientFormModal() {
  const {
    createModalOpen,
    openCreate,
    closeCreate,
    beginCreate,
    createSelection,
    createSubmitting,
    createError,
    submitCreate,
    schema,
    loading,
  } = useIbv()

  const picking = createSelection === null

  return (
    <Dialog open={createModalOpen} onOpenChange={(o) => (o ? null : closeCreate())}>
      <DialogContent
        showCloseButton
        className={
          picking
            ? "flex max-h-[92vh] flex-col gap-0 p-0 sm:max-w-[480px]"
            : "flex max-h-[92vh] w-[96vw] max-w-[1200px] flex-col gap-0 p-0"
        }
      >
        <DialogHeader className="border-b border-border p-4">
          {createSelection && (
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {humanizeSegment(createSelection.insurance_type)}
            </p>
          )}
          <DialogTitle>
            {picking ? "Add Patient Form" : schema?.name ?? "New Patient Form"}
          </DialogTitle>
          <DialogDescription>
            {picking
              ? "Choose the form type to create."
              : "Fill in the patient details. Fields marked as system fields are required."}
          </DialogDescription>
        </DialogHeader>

        {picking ? (
          <SchemaPicker
            createError={createError}
            loading={loading}
            onContinue={(option) => void beginCreate(option)}
            onCancel={closeCreate}
          />
        ) : (
          <>
            {createError && (
              <p
                className="border-b border-border bg-destructive/5 px-4 py-2 text-sm text-destructive"
                role="alert"
              >
                {createError}
              </p>
            )}
            <div className="flex-1 overflow-auto bg-[#f8f9fa] p-4 font-ibv">
              <SchemaForm />
            </div>
            <div className="flex items-center justify-between gap-4 border-t border-border p-4">
              <Button variant="outline" onClick={openCreate} disabled={createSubmitting}>
                Back
              </Button>
              <div className="flex items-center gap-3">
                <Button
                  variant="outline"
                  onClick={closeCreate}
                  disabled={createSubmitting}
                  className="min-w-[140px] border-ibv-row bg-white text-foreground hover:bg-muted/50"
                >
                  Cancel
                </Button>
                <Button
                  onClick={() => void submitCreate()}
                  disabled={createSubmitting}
                  className="min-w-[140px]"
                >
                  {createSubmitting ? "Submitting…" : "Submit"}
                </Button>
              </div>
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
