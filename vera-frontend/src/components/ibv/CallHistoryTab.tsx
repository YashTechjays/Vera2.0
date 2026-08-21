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
import { usePermission } from "@/lib/auth/permissions"
import { useIbv } from "./IbvProvider"
import { RecordingPlayer } from "./RecordingPlayer"

/** One attempt row. Props-driven (no hooks) so the play-control gating is unit-
 *  testable: the control renders only when the DTO advertises a playable
 *  recording AND the caller holds recordings:read. */
export function AttemptCard({
  attempt: a,
  retriedAttempt,
  expanded,
  onToggleFields,
  canPlay,
  playerOpen,
  onTogglePlayer,
}: {
  attempt: CallAttempt
  retriedAttempt: number | undefined
  expanded: boolean
  onToggleFields: () => void
  canPlay: boolean
  playerOpen: boolean
  onTogglePlayer: () => void
}) {
  return (
    <div className="rounded-md border border-border bg-white p-3">
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
        {retriedAttempt !== undefined && (
          <span className="text-xs text-muted-foreground">retry of attempt {retriedAttempt}</span>
        )}
        {a.authoritative === false && (
          <span
            className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-amber-700"
            title="Nothing ties this call to a payer-side record — the answers are unverified, but a reviewer may still accept them."
          >
            No call reference — unverified
          </span>
        )}
      </div>
      <button
        type="button"
        className="mt-1 text-xs text-muted-foreground underline-offset-2 hover:underline disabled:no-underline"
        disabled={a.changed_paths.length === 0}
        onClick={onToggleFields}
      >
        {a.changed_paths.length} field{a.changed_paths.length === 1 ? "" : "s"} updated
      </button>
      {expanded && (
        <ul className="mt-1 list-inside list-disc text-xs text-muted-foreground">
          {a.changed_paths.map((p) => (
            <li key={p}>{fieldLabel(p)}</li>
          ))}
        </ul>
      )}
      {canPlay && a.recording_available && (
        <button
          type="button"
          className="mt-1 block text-xs text-muted-foreground underline-offset-2 hover:underline"
          onClick={onTogglePlayer}
        >
          {playerOpen ? "Hide recording" : "Play recording"}
        </button>
      )}
      {playerOpen && <RecordingPlayer callId={a.id} />}
    </div>
  )
}

/** The form's call-attempt timeline — fetched once per modal open. */
export function CallHistoryTab() {
  const { formId } = useIbv()
  const canPlayRecordings = usePermission("recordings:read")
  const [attempts, setAttempts] = useState<CallAttempt[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  // One player at a time: opening another attempt's player collapses (and thus
  // silences) the previous one.
  const [openPlayerId, setOpenPlayerId] = useState<string | null>(null)

  useEffect(() => {
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
      {attempts.map((a) => (
        <AttemptCard
          key={a.id}
          attempt={a}
          retriedAttempt={
            a.retry_of ? attempts.find((p) => p.id === a.retry_of)?.attempt : undefined
          }
          expanded={!!expanded[a.id]}
          onToggleFields={() => setExpanded((e) => ({ ...e, [a.id]: !e[a.id] }))}
          canPlay={canPlayRecordings}
          playerOpen={openPlayerId === a.id}
          onTogglePlayer={() => setOpenPlayerId((id) => (id === a.id ? null : a.id))}
        />
      ))}
    </div>
  )
}
