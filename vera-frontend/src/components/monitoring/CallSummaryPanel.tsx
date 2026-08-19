import { useEffect, useState } from "react"
import { FileText, RefreshCw } from "lucide-react"

import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api/client"
import { summaryText } from "@/lib/monitoring/summaryText"
import {
  getCallSummary,
  type LiveCallSummary,
  type LiveCallSummarySections,
} from "@/lib/api/calls"

function errorMessage(e: unknown): string {
  if (e instanceof ApiError && e.httpStatus === 503) {
    return "Summary temporarily unavailable — try again shortly."
  }
  return e instanceof ApiError ? e.message : "Could not load the summary."
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </h4>
      {children}
    </section>
  )
}

/** The skimmable layout: labeled sections with real bullet lists, the next step
 *  called out last since it's what a supervisor taking over needs first. */
function SectionedSummary({ sections }: { sections: LiveCallSummarySections }) {
  return (
    <div className="space-y-4 text-sm leading-relaxed text-foreground">
      {sections.participants && (
        <Section title="Participants">
          <p>{sections.participants}</p>
        </Section>
      )}
      {sections.purpose && (
        <Section title="Purpose">
          <p>{sections.purpose}</p>
        </Section>
      )}
      {sections.facts.length > 0 && (
        <Section title="Established so far">
          <ul className="list-disc space-y-1 pl-5">
            {sections.facts.map((fact) => (
              <li key={fact}>{fact}</li>
            ))}
          </ul>
        </Section>
      )}
      {sections.open_items.length > 0 && (
        <Section title="Open items">
          <ul className="list-disc space-y-1 pl-5">
            {sections.open_items.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </Section>
      )}
      {sections.next_step && (
        <Section title="Next step">
          <p className="rounded-md border-l-2 border-primary bg-primary/5 px-3 py-2 font-medium">
            {sections.next_step}
          </p>
        </Section>
      )}
    </div>
  )
}

/**
 * On-demand supervisor-handoff summary of the call so far (the Summary tab of
 * Live Monitoring). Fetches on mount — the server caches for a few seconds, so
 * flipping tabs back and forth is cheap — with a manual refresh for a live call
 * that has moved on.
 * PHI hygiene: the summary is held in component state only and discarded on
 * unmount (switching tabs / closing the modal). Never persisted or logged.
 */
export function CallSummaryPanel({
  callId,
  onTextChange,
}: {
  callId: string
  /** Plain-text rendering of the summary, for the modal's copy button. */
  onTextChange?: (text: string) => void
}) {
  const [result, setResult] = useState<LiveCallSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Bumped by the refresh button; the fetch effect re-runs on it.
  const [fetchNonce, setFetchNonce] = useState(0)

  useEffect(() => {
    onTextChange?.(summaryText(result))
  }, [result, onTextChange])

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
    if (result?.sections) {
      return <SectionedSummary sections={result.sections} />
    }
    // The LLM ignored the JSON contract — the plain-text summary still reads fine.
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
