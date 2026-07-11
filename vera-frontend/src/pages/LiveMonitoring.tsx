import { useEffect, useMemo, useState } from "react"
import { ArrowUp, Check } from "lucide-react"

import { Card } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"
import { useIbv } from "@/components/ibv/IbvProvider"
import { usePermission } from "@/lib/auth/permissions"
import { endCall, listCalls, publishCall, type CallSummary } from "@/lib/api/calls"
import { ApiError } from "@/lib/api/client"
import { CallOverviewModal } from "@/components/monitoring/CallOverviewModal"
import { InterveneModal } from "@/components/monitoring/InterveneModal"
import { stats, type CallCategory, type LiveCall } from "@/lib/mock-data"

// Re-poll the active list so a VA learns about newly published calls.
const POLL_MS = 8000

type TabKey = "active" | "critical"
// No Completed tab: GET /calls only carries live calls; history is a follow-up.
const TABS: { key: TabKey; label: string }[] = [
  { key: "active", label: "Active" },
  { key: "critical", label: "Critical" },
]

function categoryOf(status: string): CallCategory {
  const s = status.toLowerCase()
  if (s === "critical") return "critical"
  if (s === "completed" || s === "failed") return "completed"
  if (s === "waiting" || s === "ivr") return "processing"
  return "active"
}

const rowTint: Record<CallCategory, string> = {
  critical: "bg-red-50",
  active: "",
  processing: "bg-amber-50",
  completed: "",
}
const durationColor: Record<CallCategory, string> = {
  critical: "text-red-600",
  active: "text-emerald-600",
  processing: "text-amber-600",
  completed: "text-muted-foreground",
}
const badgeStyle: Record<CallCategory, string> = {
  critical: "bg-red-100 text-red-700",
  active: "bg-emerald-100 text-emerald-700",
  processing: "bg-amber-100 text-amber-800",
  completed: "bg-emerald-100 text-emerald-700",
}

/** mm:ss elapsed since the call started (— until it has). */
function elapsed(startedAt: string | null, now: number): string {
  if (!startedAt) return "—"
  const secs = Math.max(0, Math.floor((now - Date.parse(startedAt)) / 1000))
  const m = Math.floor(secs / 60)
  const s = secs % 60
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
}

/** Adapt a real call into the shape the overview/intervene modals render. The
 *  `id` is the real call id so the modal can mint a join token. Fields the API
 *  doesn't provide yet (insurance, confidence, form %) are placeholders. */
function toLiveCall(c: CallSummary, now: number): LiveCall {
  return {
    id: c.id,
    patient: c.patient_name || "—",
    type: "Patient",
    agent: "—",
    duration: elapsed(c.started_at, now),
    status: c.status,
    category: categoryOf(c.status),
    visible: c.published,
    action: c.is_owner ? "view" : "intervene",
    insurance: "—",
    confidence: 0,
    formProgress: 0,
    callTime: elapsed(c.started_at, now),
  }
}

function CallIndicator({ category }: { category: CallCategory }) {
  if (category === "critical")
    return <ArrowUp className="size-4 text-red-500" strokeWidth={2.5} />
  if (category === "active")
    return <ArrowUp className="size-4 text-emerald-500" strokeWidth={2.5} />
  return <Check className="size-4 text-emerald-500" strokeWidth={2.5} />
}

export function LiveMonitoring() {
  const { openForm } = useIbv()
  const canPublish = usePermission("calls:publish")
  // PHI (patient_name) stays in component state so it's discarded on unmount.
  const [calls, setCalls] = useState<CallSummary[]>([])
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<TabKey>("active")
  const [now, setNow] = useState(() => Date.now())
  const [publishing, setPublishing] = useState<string | null>(null)
  const [modalCall, setModalCall] = useState<LiveCall | null>(null)
  const [overviewOpen, setOverviewOpen] = useState(false)
  const [interveneOpen, setInterveneOpen] = useState(false)
  const [ending, setEnding] = useState(false)

  // Load + poll (skip while the tab is hidden).
  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const items = await listCalls()
        if (!cancelled) {
          setCalls(items)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not load calls.")
      }
    }
    void load()
    const id = setInterval(() => {
      if (document.visibilityState === "visible") void load()
    }, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  // Tick so Duration advances between polls.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const rows = useMemo(() => {
    if (tab === "critical") return calls.filter((c) => categoryOf(c.status) === "critical")
    return calls
  }, [tab, calls])

  async function onPublish(call: CallSummary) {
    setPublishing(call.id)
    try {
      const updated = await publishCall(call.id)
      setCalls((cs) => cs.map((c) => (c.id === updated.id ? updated : c)))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not publish the call.")
    } finally {
      setPublishing(null)
    }
  }

  function openOverview(call: CallSummary) {
    setModalCall(toLiveCall(call, now))
    setOverviewOpen(true)
  }

  // Ends the call for real: the backend deletes the LiveKit room (hanging up the
  // SIP leg and shutting the agent down) and its pipeline completes the call.
  // Optimistically drop the row on success (the poll re-syncs); either way close
  // the modals, so a failure's error banner is visible behind them.
  async function onEndCall() {
    const id = modalCall?.id
    if (!id || ending) return
    setEnding(true)
    try {
      await endCall(id)
      setCalls((cs) => cs.filter((c) => c.id !== id))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not end the call.")
    } finally {
      setEnding(false)
      setOverviewOpen(false)
      setInterveneOpen(false)
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Live Monitoring</h1>

      {/* Stat cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {stats.map(({ label, value, icon: Icon, tone }) => (
          <Card key={label}>
            <div className="flex items-center gap-3 px-4">
              <div className="flex size-11 shrink-0 items-center justify-center rounded-md bg-muted">
                <Icon
                  className={cn("size-5", tone === "critical" ? "text-red-500" : "text-[#34B2B2]")}
                />
              </div>
              <div>
                <div
                  className={cn(
                    "text-2xl font-bold leading-tight",
                    tone === "critical" && "text-red-600",
                  )}
                >
                  {value}
                </div>
                <div className="text-sm text-muted-foreground">{label}</div>
              </div>
            </div>
          </Card>
        ))}
      </div>

      <h2 className="text-lg font-semibold tracking-tight">Patient Call Status</h2>

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <Card>
        <div className="px-4">
          <div className="flex items-center gap-1">
            {TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => setTab(t.key)}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                  tab === t.key
                    ? "bg-foreground text-background"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>

        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="pl-10">Patient Name</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Agent</TableHead>
              <TableHead>Duration</TableHead>
              <TableHead>Call Status</TableHead>
              <TableHead>Visible To All</TableHead>
              <TableHead>Action</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((call) => {
              const cat = categoryOf(call.status)
              return (
                <TableRow key={call.id} className={cn(rowTint[cat])}>
                  <TableCell className="font-medium">
                    <span className="flex items-center gap-2">
                      <CallIndicator category={cat} />
                      <span className="capitalize">{call.patient_name || "—"}</span>
                    </span>
                  </TableCell>
                  <TableCell className="text-muted-foreground">—</TableCell>
                  <TableCell className="text-muted-foreground">—</TableCell>
                  <TableCell className={cn("font-semibold tabular-nums", durationColor[cat])}>
                    {elapsed(call.started_at, now)}
                  </TableCell>
                  <TableCell>
                    <span
                      className={cn(
                        "inline-block max-w-[190px] truncate rounded-md px-2 py-1 text-xs font-medium",
                        badgeStyle[cat],
                      )}
                      title={call.status}
                    >
                      {call.status}
                    </span>
                  </TableCell>
                  <TableCell>
                    {/* One-way: only the owner can flip it, and only on → publish. */}
                    <Switch
                      checked={call.published}
                      disabled={
                        call.published || !call.is_owner || !canPublish || publishing === call.id
                      }
                      onCheckedChange={(v) => {
                        if (v) void onPublish(call)
                      }}
                    />
                  </TableCell>
                  <TableCell>
                    <Button
                      size="sm"
                      variant={call.is_owner ? "default" : "outline"}
                      onClick={() => openOverview(call)}
                    >
                      {call.is_owner ? "View Live" : "Intervene"}
                    </Button>
                  </TableCell>
                </TableRow>
              )
            })}
            {rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={7} className="py-10 text-center text-muted-foreground">
                  No calls in this view.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </Card>

      <CallOverviewModal
        call={modalCall}
        open={overviewOpen}
        onOpenChange={setOverviewOpen}
        onExpand={() => openForm()}
        onIntervene={() => {
          setOverviewOpen(false)
          setInterveneOpen(true)
        }}
        onEndCall={onEndCall}
        ending={ending}
      />

      <InterveneModal
        call={modalCall}
        open={interveneOpen}
        onOpenChange={setInterveneOpen}
        onEndCall={onEndCall}
        ending={ending}
      />
    </div>
  )
}
