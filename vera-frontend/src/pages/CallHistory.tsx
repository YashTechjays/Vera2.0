import { useEffect, useState } from "react"
import { Search } from "lucide-react"

import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api/client"
import { usePermission } from "@/lib/auth/permissions"
import { useIbv } from "@/components/ibv/IbvProvider"
import { RecordingPlayer } from "@/components/ibv/RecordingPlayer"
import { listCallHistory, type CallHistoryRow } from "@/lib/api/calls"
import { formatDateTime, modeBadgeClass, statusLabel } from "@/lib/patient-forms/display"

const PAGE_SIZE = 20
const COLUMN_COUNT = 6

// The call-status values the dispatcher/worker record. Offered as filter options;
// the list itself shows whatever status a row carries.
const STATUS_OPTIONS = [
  "initiated",
  "ringing",
  "ivr",
  "active",
  "waiting",
  "critical",
  "completed",
  "canceled",
  "no_answer",
  "busy",
  "failed",
]

/** Chip classes per call status — terminal outcomes colored by disposition. */
function callStatusBadgeClass(status: string): string {
  switch (status) {
    case "completed":
      return "bg-emerald-100 text-emerald-700"
    case "busy":
    case "no_answer":
    case "failed":
      return "bg-red-100 text-red-700"
    case "canceled":
      return "bg-slate-100 text-slate-600"
    case "critical":
      return "bg-amber-100 text-amber-700"
    default:
      return "bg-blue-100 text-blue-700"
  }
}

export function CallHistory() {
  const canRead = usePermission("calls:read")
  const canPlay = usePermission("recordings:read")
  const { openFormById } = useIbv()

  const [items, setItems] = useState<CallHistoryRow[] | null>(null)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [status, setStatus] = useState("")
  const [query, setQuery] = useState("")
  const [dateFrom, setDateFrom] = useState("")
  const [dateTo, setDateTo] = useState("")
  const [error, setError] = useState<string | null>(null)
  // One player at a time: opening another row's player collapses (and silences) the prior one.
  const [openPlayerId, setOpenPlayerId] = useState<string | null>(null)

  useEffect(() => {
    if (!canRead) return
    let cancelled = false
    listCallHistory({
      page,
      page_size: PAGE_SIZE,
      status: status || undefined,
      q: query.trim() || undefined,
      // The date inputs are calendar days; widen to the full UTC day each bounds.
      date_from: dateFrom ? `${dateFrom}T00:00:00Z` : undefined,
      date_to: dateTo ? `${dateTo}T23:59:59Z` : undefined,
    })
      .then((res) => {
        if (cancelled) return
        setItems(res.items)
        setTotal(res.total)
        setError(null)
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load call history.")
          setItems([])
        }
      })
    return () => {
      cancelled = true
    }
  }, [canRead, page, status, query, dateFrom, dateTo])

  if (!canRead) {
    return (
      <div className="p-6">
        <h1 className="text-xl font-semibold">Call History</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          You don't have permission to view call history.
        </p>
      </div>
    )
  }

  const rows = items ?? []
  const lastPage = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Call History</h1>

      <Card>
        <div className="flex flex-wrap items-center justify-end gap-3 px-4 pt-4">
          <form
            className="relative w-56"
            onSubmit={(e) => {
              e.preventDefault()
              setPage(1)
            }}
          >
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search patient / policy"
              className="pl-8"
            />
          </form>
          <div className="w-40">
            <Select
              value={status}
              onChange={(e) => {
                setStatus(e.target.value)
                setPage(1)
              }}
            >
              <option value="">All Status</option>
              {STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {statusLabel(s)}
                </option>
              ))}
            </Select>
          </div>
          <Input
            type="date"
            aria-label="From date"
            value={dateFrom}
            onChange={(e) => {
              setDateFrom(e.target.value)
              setPage(1)
            }}
            className="w-40"
          />
          <Input
            type="date"
            aria-label="To date"
            value={dateTo}
            onChange={(e) => {
              setDateTo(e.target.value)
              setPage(1)
            }}
            className="w-40"
          />
        </div>

        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-6">Date / Time</TableHead>
              <TableHead>Patient</TableHead>
              <TableHead>Policy / Member</TableHead>
              <TableHead>Provider</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Mode</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items === null && (
              <TableRow>
                <TableCell colSpan={COLUMN_COUNT} className="py-10 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {items?.length === 0 && (
              <TableRow>
                <TableCell colSpan={COLUMN_COUNT} className="py-10 text-center text-muted-foreground">
                  No calls match your filters.
                </TableCell>
              </TableRow>
            )}
            {rows.map((c) => (
              <CallRow
                key={c.id}
                call={c}
                canPlay={canPlay}
                playerOpen={openPlayerId === c.id}
                onOpenForm={() => openFormById(c.form_id)}
                onTogglePlayer={() => setOpenPlayerId((id) => (id === c.id ? null : c.id))}
              />
            ))}
          </TableBody>
        </Table>

        {error && (
          <p className="px-4 py-3 text-sm text-destructive" role="alert">
            {error}
          </p>
        )}

        <div className="flex items-center justify-between gap-4 px-4 py-3">
          <span className="text-sm text-muted-foreground">
            {items
              ? `${total} call${total === 1 ? "" : "s"} · page ${page} of ${lastPage}`
              : "Loading…"}
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={page >= lastPage}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      </Card>
    </div>
  )
}

/** One call row plus, when its player is open, an expanded row hosting the inline
 *  audio. Props-driven so the play-control gating stays trivially testable (mirrors
 *  the per-form AttemptCard). */
export function CallRow({
  call: c,
  canPlay,
  playerOpen,
  onOpenForm,
  onTogglePlayer,
}: {
  call: CallHistoryRow
  canPlay: boolean
  playerOpen: boolean
  onOpenForm: () => void
  onTogglePlayer: () => void
}) {
  return (
    <>
      <TableRow className="cursor-pointer" onClick={onOpenForm}>
        <TableCell className="pl-6">{formatDateTime(c.created_at)}</TableCell>
        <TableCell className="font-medium capitalize">{c.patient_name || "—"}</TableCell>
        <TableCell className="text-muted-foreground">{c.member_id || "—"}</TableCell>
        <TableCell className="text-muted-foreground">{c.insurance_provider || "—"}</TableCell>
        <TableCell>
          <span
            className={cn(
              "inline-block rounded-full px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide",
              callStatusBadgeClass(c.status),
            )}
          >
            {statusLabel(c.status)}
          </span>
        </TableCell>
        <TableCell>
          <div className="flex items-center gap-2">
            <span
              className={cn(
                "inline-block rounded-full px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide",
                modeBadgeClass(c.mode),
              )}
            >
              {c.mode}
            </span>
            {canPlay && c.recording_available && (
              <button
                type="button"
                className="text-xs text-muted-foreground underline-offset-2 hover:underline"
                // Stop the row's open-form click from firing when toggling the player.
                onClick={(e) => {
                  e.stopPropagation()
                  onTogglePlayer()
                }}
              >
                {playerOpen ? "Hide recording" : "Play recording"}
              </button>
            )}
          </div>
        </TableCell>
      </TableRow>
      {playerOpen && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={COLUMN_COUNT} className="pl-6">
            <RecordingPlayer callId={c.id} />
          </TableCell>
        </TableRow>
      )}
    </>
  )
}
