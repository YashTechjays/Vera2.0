import { useState } from "react"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { cn, triggerBlobDownload } from "@/lib/utils"
import { usePermission } from "@/lib/auth/permissions"
import { ApiError } from "@/lib/api/errors"
import {
  allowedStatusTransitions,
  humanizeSegment,
  statusActionLabel,
  statusBadgeClass,
  statusLabel,
} from "@/lib/patient-forms/display"
import { exportPatientForm } from "@/lib/patient-forms/api"
import { useIbv } from "./IbvProvider"
import { SchemaForm } from "./SchemaForm"
import { CallHistoryTab } from "./CallHistoryTab"

const TABS = [
  { id: "form", label: "Form" },
  { id: "calls", label: "Call history" },
] as const

type TabId = (typeof TABS)[number]["id"]

export function IbvFormModal() {
  const {
    schema,
    formId,
    modalOpen,
    closeForm,
    dirty,
    saveState,
    save,
    resolveAll,
    pendingDisputeCount,
    loading,
    error,
    clearedRequired,
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
  } = useIbv()
  const canWrite = usePermission("forms:write")
  const canExport = usePermission("forms:export")
  const transitions = status ? allowedStatusTransitions(status) : []
  const canExportForm = canExport && status === "completed" && !!formId
  const [tab, setTab] = useState<TabId>("form")
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)

  async function handleExport() {
    if (!formId) return
    setExporting(true)
    setExportError(null)
    try {
      const blob = await exportPatientForm(formId)
      triggerBlobDownload(blob, `ibv-${formId}.xlsx`)
    } catch (err) {
      setExportError(err instanceof ApiError ? err.message : "Export failed.")
    } finally {
      setExporting(false)
    }
  }

  return (
    <Dialog open={modalOpen} onOpenChange={(o) => (o ? null : closeForm())}>
      <DialogContent
        showCloseButton
        className="flex max-h-[92vh] w-[96vw] max-w-[1200px] flex-col gap-0 p-0"
        // Fires on every open (Radix), so each session starts on the Form tab.
        onOpenAutoFocus={() => setTab("form")}
      >
        <DialogHeader className="border-b border-border p-4">
          {/* Form-type eyebrow, from the loaded form's insurance type. */}
          {insuranceType && (
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              {humanizeSegment(insuranceType)}
            </p>
          )}
          {/* Title from the fetched schema document — never a hardcoded form name. */}
          <DialogTitle>
            {[schema?.name, patientName].filter(Boolean).join(" — ") ||
              "Patient Form"}
          </DialogTitle>
          <DialogDescription>
            Review captured values and resolve disputes.
          </DialogDescription>
        </DialogHeader>

        {status && (
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-muted/30 px-4 py-2">
            <div className="flex items-center gap-2 text-sm">
              <span className="text-muted-foreground">Status</span>
              <span
                className={cn(
                  "inline-block rounded-full px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide",
                  statusBadgeClass(status),
                )}
              >
                {statusLabel(status)}
              </span>
            </div>
            {((canWrite && transitions.length > 0) || canExportForm) && (
              <div className="flex items-center gap-2">
                {transitions.includes("in_queue") && (
                  <>
                    <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      <Switch
                        checked={ivrNavigation}
                        onCheckedChange={setIvrNavigation}
                        disabled={statusChanging}
                      />
                      IVR navigation
                    </label>
                    {/* Canonicalizes the form's provider so dispatch applies the
                        right IVR playbook; pre-selected from the intake string. */}
                    <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      Provider
                      <div className="w-44">
                        <Select
                          value={providerId}
                          onChange={(e) => setProviderId(e.target.value)}
                          disabled={statusChanging}
                          className="text-xs"
                          aria-label="Insurance provider"
                        >
                          <option value="">Auto-detect from intake</option>
                          {providers.map((p) => (
                            <option key={p.id} value={p.id}>
                              {p.name}
                            </option>
                          ))}
                        </Select>
                      </div>
                    </label>
                  </>
                )}
                {canExportForm && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={exporting}
                    onClick={() => void handleExport()}
                  >
                    {exporting ? "Exporting…" : "Export XLSX"}
                  </Button>
                )}
                {canWrite &&
                  transitions.map((target) => (
                    <Button
                      key={target}
                      size="sm"
                      variant={target === "completed" ? "default" : "outline"}
                      disabled={statusChanging}
                      onClick={() => changeStatus(target)}
                    >
                      {statusActionLabel(target)}
                    </Button>
                  ))}
              </div>
            )}
          </div>
        )}
        {statusError && (
          <p
            className="border-b border-border bg-destructive/5 px-4 py-2 text-sm text-destructive"
            role="alert"
          >
            {statusError}
          </p>
        )}
        {exportError && (
          <p
            className="border-b border-border bg-destructive/5 px-4 py-2 text-sm text-destructive"
            role="alert"
          >
            {exportError}
          </p>
        )}

        <div className="flex gap-1 border-b border-border px-4 pt-2">
          {TABS.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              onClick={() => setTab(id)}
              className={cn(
                "rounded-t-md px-3 py-1.5 text-sm font-medium",
                tab === id
                  ? "border border-b-0 border-border bg-background"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {label}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-auto bg-[#f8f9fa] p-4 font-ibv">
          {loading && <p className="text-sm text-muted-foreground">Loading…</p>}
          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}
          {/* Keyed by form so the tab's fetch/expansion state never leaks across forms. */}
          {!loading &&
            !error &&
            (tab === "form" ? <SchemaForm /> : <CallHistoryTab key={formId ?? "demo"} />)}
        </div>

        <div className="flex items-center justify-between gap-4 border-t border-border p-4">
          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={pendingDisputeCount === 0}
              onCheckedChange={(c) => {
                if (c) resolveAll()
              }}
              disabled={pendingDisputeCount === 0}
            />
            Resolve all disputes
            {pendingDisputeCount > 0 && (
              <span className="text-muted-foreground">
                ({pendingDisputeCount} pending)
              </span>
            )}
          </label>
          <div className="flex items-center gap-3">
            {saveState === "saved" && !dirty && (
              <span className="text-sm text-emerald-600">Saved</span>
            )}
            <Button
              variant="outline"
              onClick={closeForm}
              className="min-w-[140px] border-ibv-row bg-white text-foreground hover:bg-muted/50"
            >
              Cancel
            </Button>
            <Button
              onClick={save}
              disabled={!dirty || saveState === "saving" || clearedRequired.length > 0}
              title={
                clearedRequired.length > 0
                  ? "Restore the cleared required fields before saving."
                  : undefined
              }
              className="min-w-[140px]"
            >
              {saveState === "saving" ? "Saving…" : "Save"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
