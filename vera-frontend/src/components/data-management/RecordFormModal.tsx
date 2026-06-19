import { useEffect, useRef, useState } from "react"
import { ChevronDown, ChevronUp } from "lucide-react"

import {
  Dialog,
  DialogContent,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { SchemaForm } from "@/components/ibv/SchemaForm"
import {
  patientStatusStyles,
  statusTransitions,
  type PatientForm,
  type PatientFormStatus,
} from "@/lib/mock-data"

/**
 * Patient-form detail modal (matches smart-caller-fe's Data Management modal):
 * shows the verification form with a status-change dropdown in the header that
 * only offers the transitions allowed for the record's current status.
 */
export function RecordFormModal({
  record,
  open,
  onOpenChange,
  onStatusChange,
}: {
  record: PatientForm | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onStatusChange: (id: string, status: PatientFormStatus) => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  // Close the status menu on outside click.
  useEffect(() => {
    if (!menuOpen) return
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node))
        setMenuOpen(false)
    }
    document.addEventListener("mousedown", onClick)
    return () => document.removeEventListener("mousedown", onClick)
  }, [menuOpen])

  // Closing the dialog also dismisses the status menu, so a fresh open starts
  // clean (the component stays mounted between opens).
  const handleOpenChange = (next: boolean) => {
    if (!next) setMenuOpen(false)
    onOpenChange(next)
  }

  const transitions = record ? statusTransitions[record.status] : []
  const canChange = transitions.length > 0

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        showCloseButton
        className="flex max-h-[92vh] w-[96vw] max-w-[1100px] flex-col gap-0 p-0"
      >
        {/* Header */}
        <div className="flex items-center justify-between gap-4 border-b border-border p-4 pr-12">
          <div>
            <DialogTitle className="text-lg font-semibold">
              {record?.patientName ?? "—"}
            </DialogTitle>
            <p className="text-sm text-muted-foreground">
              {record?.chartNo} · {record?.insuranceProvider}
            </p>
          </div>

          {/* Status-change dropdown */}
          <div className="relative" ref={menuRef}>
            <button
              type="button"
              disabled={!canChange}
              onClick={() => canChange && setMenuOpen((v) => !v)}
              className={cn(
                "flex w-[210px] items-center justify-between gap-2 rounded-md px-3 py-2 text-xs font-semibold uppercase tracking-wide",
                record && patientStatusStyles[record.status],
                canChange ? "cursor-pointer" : "cursor-not-allowed opacity-60"
              )}
            >
              <span>{record?.status}</span>
              {canChange &&
                (menuOpen ? (
                  <ChevronUp className="size-3.5 shrink-0" />
                ) : (
                  <ChevronDown className="size-3.5 shrink-0" />
                ))}
            </button>

            {menuOpen && canChange && (
              <div className="absolute top-full right-0 z-50 mt-1 w-[210px] overflow-hidden rounded-md border border-border bg-background shadow-lg">
                {transitions.map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => {
                      if (record) onStatusChange(record.id, s)
                      setMenuOpen(false)
                    }}
                    className="block w-full px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-foreground hover:bg-muted"
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Body — verification form */}
        <div className="min-h-[320px] flex-1 overflow-auto bg-[#f8f9fa] p-4 font-ibv">
          <SchemaForm />
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 border-t border-border p-4">
          <Button
            variant="outline"
            onClick={() => handleOpenChange(false)}
            className="min-w-[140px] border-ibv-row bg-white text-foreground hover:bg-muted/50"
          >
            Cancel
          </Button>
          <Button onClick={() => handleOpenChange(false)} className="min-w-[140px]">
            Save
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
