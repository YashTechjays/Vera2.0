import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"
import { usePermission } from "@/lib/auth/permissions"
import {
  allowedStatusTransitions,
  humanizeSegment,
  statusActionLabel,
  statusBadgeClass,
  statusLabel,
} from "@/lib/patient-forms/display"
import { useIbv } from "./IbvProvider"
import { SchemaForm } from "./SchemaForm"

export function IbvFormModal() {
  const {
    schema,
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
    statusError,
    statusChanging,
    insuranceType,
  } = useIbv()
  const canWrite = usePermission("forms:write")
  const transitions = status ? allowedStatusTransitions(status) : []

  return (
    <Dialog open={modalOpen} onOpenChange={(o) => (o ? null : closeForm())}>
      <DialogContent
        showCloseButton
        className="flex max-h-[92vh] w-[96vw] max-w-[1200px] flex-col gap-0 p-0"
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
            {canWrite && transitions.length > 0 && (
              <div className="flex items-center gap-2">
                {transitions.includes("in_queue") && (
                  <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <Switch
                      checked={ivrNavigation}
                      onCheckedChange={setIvrNavigation}
                      disabled={statusChanging}
                    />
                    IVR navigation
                  </label>
                )}
                {transitions.map((target) => (
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

        <div className="flex-1 overflow-auto bg-[#f8f9fa] p-4 font-ibv">
          {loading && <p className="text-sm text-muted-foreground">Loading…</p>}
          {error && (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}
          {!loading && !error && <SchemaForm />}
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
