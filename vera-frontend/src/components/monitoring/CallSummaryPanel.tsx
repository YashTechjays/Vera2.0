import { useEffect, useState } from "react"
import { FileText, RefreshCw } from "lucide-react"

import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api/client"
import { getCallSummary, type LiveCallSummary } from "@/lib/api/calls"

function errorMessage(e: unknown): string {
  if (e instanceof ApiError && e.httpStatus === 503) {
    return "Summary temporarily unavailable — try again shortly."
  }
  return e instanceof ApiError ? e.message : "Could not load the summary."
}

/**
 * On-demand supervisor-handoff summary of the call so far (the Summary tab of
 * Live Monitoring). Fetches on mount — the server caches for a few seconds, so
 * flipping tabs back and forth is cheap — with a manual refresh for a live call
 * that has moved on.
 * PHI hygiene: the summary is held in component state only and discarded on
 * unmount (switching tabs / closing the modal). Never persisted or logged.
 */
export function CallSummaryPanel({ callId }: { callId: string }) {
  const [result, setResult] = useState<LiveCallSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Bumped by the refresh button; the fetch effect re-runs on it.
  const [fetchNonce, setFetchNonce] = useState(0)

  useEffect(() => {
    let stale = false
    getCallSummary(callId)
      .then((r) => {
        if (stale) return
        setResult(r)
        setError(null)
      })
      .catch((e: unknown) => {
        if (!stale) setError(errorMessage(e))
      })
      .finally(() => {
        if (!stale) setLoading(false)
      })
    return () => {
      stale = true
    }
  }, [callId, fetchNonce])

  function refresh() {
    setLoading(true)
    setError(null)
    setFetchNonce((n) => n + 1)
  }

  function renderBody() {
    if (loading && !result) {
      return (
        <div className="flex flex-col items-center justify-center gap-2 py-10 text-muted-foreground">
          <FileText className="size-8 animate-pulse opacity-30" />
          <span className="text-sm">Summarizing the conversation…</span>
        </div>
      )
    }
    if (error) {
      return (
        <div className="flex flex-col items-center justify-center gap-3 py-10">
          <p className="text-sm text-muted-foreground">{error}</p>
          <button
            type="button"
            onClick={refresh}
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-muted"
          >
            Try again
          </button>
        </div>
      )
    }
    if (result?.status === "pending") {
      return (
        <div className="flex flex-col items-center justify-center gap-2 py-10 text-muted-foreground">
          <FileText className="size-8 opacity-30" />
          <span className="text-sm">Not enough conversation to summarize yet.</span>
        </div>
      )
    }
    return (
      <p className="whitespace-pre-wrap text-sm leading-relaxed text-foreground">
        {result?.summary}
      </p>
    )
  }

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-border px-4 py-2">
        <span className="text-xs text-muted-foreground">
          {result?.status === "ready"
            ? `Summary of ${result.turn_count} turns · ${new Date(result.generated_at).toLocaleTimeString()}`
            : "Handoff context for taking over this call"}
        </span>
        <button
          type="button"
          onClick={refresh}
          disabled={loading}
          title="Refresh summary"
          className="flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-muted disabled:opacity-50"
        >
          <RefreshCw className={cn("size-4", loading && "animate-spin")} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-4">{renderBody()}</div>
    </div>
  )
}
