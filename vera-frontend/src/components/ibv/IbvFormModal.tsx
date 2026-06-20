import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { useIbv } from "./IbvProvider"
import { SchemaForm } from "./SchemaForm"

export function IbvFormModal() {
  const {
    modalOpen,
    closeForm,
    dirty,
    saveState,
    save,
    resolveAll,
    pendingDisputeCount,
  } = useIbv()

  return (
    <Dialog open={modalOpen} onOpenChange={(o) => (o ? null : closeForm())}>
      <DialogContent
        showCloseButton
        className="flex max-h-[92vh] w-[96vw] max-w-[1200px] flex-col gap-0 p-0"
      >
        <DialogHeader className="border-b border-border p-4">
          <DialogTitle>IBV Data Entry Form</DialogTitle>
          <DialogDescription>
            Insurance Benefit Verification — review captured values and resolve
            disputes.
          </DialogDescription>
        </DialogHeader>

        <div className="flex-1 overflow-auto bg-[#f8f9fa] p-4 font-ibv">
          <SchemaForm />
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
              disabled={!dirty || saveState === "saving"}
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
