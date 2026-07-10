import { useEffect, useState } from "react"

import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api/client"
import { getPatientFormCalls } from "@/lib/patient-forms/api"
import type { CallAttempt } from "@/lib/patient-forms/types"
import {
  fieldLabel,
  formatDate,
  modeBadgeClass,
  statusLabel,
} from "@/lib/patient-forms/display"
import { useIbv } from "./IbvProvider"

/** The form's call-attempt timeline — fetched once per modal open. */
export function CallHistoryTab() {
  const { formId } = useIbv()
  const [attempts, setAttempts] = useState<CallAttempt[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  useEffect(() => {
    // A new form means a new timeline — drop the previous form's expansion state.
    setExpanded({})
    if (!formId) return
    let cancelled = false
    getPatientFormCalls(formId)
      .then((res) => {
        if (!cancelled) setAttempts(res)
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Could not load call history.")
      })
    return () => {
      cancelled = true
    }
  }, [formId])

  if (error)
    return (
      <p className="text-sm text-destructive" role="alert">
        {error}
      </p>
    )
  // The demo/mock form has no backend row, so there is nothing to fetch.
  if (attempts === null && formId)
    return <p className="text-sm text-muted-foreground">Loading…</p>
  if (attempts === null || attempts.length === 0)
    return <p className="text-sm text-muted-foreground">No calls have been made for this form.</p>

  return (
    <div className="flex flex-col gap-3">
      {attempts.map((a) => {
        // Resolve the lineage annotation: the attempt this one is a retry of.
        const retriedAttempt = a.retry_of
          ? attempts.find((p) => p.id === a.retry_of)
          : undefined
        return (
          <div key={a.id} className="rounded-md border border-border bg-white p-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-semibold">Attempt {a.attempt}</span>
              <span
                className={cn(
                  "rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide",
                  modeBadgeClass(a.mode),
                )}
              >
                {a.mode}
              </span>
              <span className="text-muted-foreground">{statusLabel(a.status)}</span>
              <span className="text-muted-foreground">·</span>
              <span className="text-muted-foreground">{formatDate(a.created_at)}</span>
              {retriedAttempt && (
                <span className="text-xs text-muted-foreground">
                  retry of attempt {retriedAttempt.attempt}
                </span>
              )}
            </div>
            <button
              type="button"
              className="mt-1 text-xs text-muted-foreground underline-offset-2 hover:underline disabled:no-underline"
              disabled={a.changed_paths.length === 0}
              onClick={() => setExpanded((e) => ({ ...e, [a.id]: !e[a.id] }))}
            >
              {a.changed_paths.length} field{a.changed_paths.length === 1 ? "" : "s"} updated
            </button>
            {expanded[a.id] && (
              <ul className="mt-1 list-inside list-disc text-xs text-muted-foreground">
                {a.changed_paths.map((p) => (
                  <li key={p}>{fieldLabel(p)}</li>
                ))}
              </ul>
            )}
          </div>
        )
      })}
    </div>
  )
}
