import { useEffect, useMemo, useState } from "react"
import {
  AlertCircle,
  ArrowUp,
  Check,
  CheckCircle2,
  Phone,
  PhoneCall,
  type LucideIcon,
} from "lucide-react"

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
import {
  getCallStats,
  listCalls,
  publishCall,
  type CallStats,
  type CallSummary,
} from "@/lib/api/calls"
import { isTerminalCallStatus } from "@/lib/api/callEvents"
import { ApiError } from "@/lib/api/client"
import { elapsed } from "@/lib/monitoring/liveTimer"
import { healthDisplay, type HealthTone } from "@/lib/monitoring/health"
import { LiveCallModal } from "@/components/monitoring/LiveCallModal"
import { NOTIFICATION_EVENT } from "@/components/notifications/NotificationsProvider"
import { stats, type CallCategory, type LiveCall } from "@/lib/mock-data"

// Re-poll the active list so a VA learns about newly published calls.
const POLL_MS = 8000

type TabKey = "active" | "critical" | "completed"
const TABS: { key: TabKey; label: string }[] = [
  { key: "active", label: "Active" },
  { key: "critical", label: "Critical" },
  { key: "completed", label: "Completed" },
]

function categoryOf(status: string): CallCategory {
  const s = status.toLowerCase()
  if (s === "critical") return "critical"
  if (isTerminalCallStatus(s)) return "completed"
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
const healthText: Record<HealthTone, string> = {
  good: "text-emerald-600",
  warn: "text-amber-600",
  bad: "text-red-600",
  unknown: "text-muted-foreground",
}

/** Adapt a real call into the modal's LiveCall shape; fields the API doesn't provide yet
 *  (confidence, form %) are placeholders. */
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
    insurance: c.insurance_provider || "—",
    confidence: 0,
    formProgress: 0,
    callTime: elapsed(c.started_at, now),
    startedAt: c.started_at,
    healthScore: c.health_score,
  }
}

function CallIndicator({ category }: { category: CallCategory }) {
  if (category === "critical")
    return <ArrowUp className="size-4 text-red-500" strokeWidth={2.5} />
  if (category === "active")
    return <ArrowUp className="size-4 text-emerald-500" strokeWidth={2.5} />
  return <Check className="size-4 text-emerald-500" strokeWidth={2.5} />
}

function CallHealthCell({ call, now }: { call: CallSummary; now: number }) {
  const health = healthDisplay(call.health_score, call.health_analyzed_at, now)
  const flag =
    call.health_flag && call.health_flag !== "none"
      ? call.health_flag.replaceAll("_", " ")
      : undefined
  return (
    <span
      className={cn(
        "font-semibold tabular-nums",
        health.stale ? "text-muted-foreground" : healthText[health.tone],
      )}
      title={flag}
    >
      {health.text}
      {health.stale && " (stale)"}
    </span>
  )
}

export function LiveMonitoring() {
  const { openFormById } = useIbv()
  const canPublish = usePermission("calls:publish")
  // PHI (patient_name) stays in component state so it's discarded on unmount.
  const [calls, setCalls] = useState<CallSummary[]>([])
  const [history, setHistory] = useState<CallSummary[]>([])
  const [stats, setStats] = useState<CallStats | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<TabKey>("active")
  const [now, setNow] = useState(() => Date.now())
  const [publishing, setPublishing] = useState<string | null>(null)
  const [selected, setSelected] = useState<CallSummary | null>(null)
  const [overviewOpen, setOverviewOpen] = useState(false)

  // Load + poll (skip while the tab is hidden); a realtime notification
  // (intervention alert) refetches immediately instead of waiting the poll out.
  useEffect(() => {
    let cancelled = false
    async function load() {
      // allSettled: a stats/history hiccup must not stall the live list (and vice versa).
      const [items, counts, past] = await Promise.allSettled([
        listCalls(),
        getCallStats(),
        tab === "completed" ? listCalls("history") : Promise.resolve(null),
      ])
      if (cancelled) return
      if (items.status === "fulfilled") {
        setCalls(items.value)
        setError(null)
      } else {
        setError(
          items.reason instanceof ApiError ? items.reason.message : "Could not load calls.",
        )
      }
      if (counts.status === "fulfilled") setStats(counts.value)
      if (past.status === "fulfilled" && past.value) setHistory(past.value)
    }
    void load()
    const id = setInterval(() => {
      if (document.visibilityState === "visible") void load()
    }, POLL_MS)
    const onNotification = () => void load()
    window.addEventListener(NOTIFICATION_EVENT, onNotification)
    return () => {
      cancelled = true
      clearInterval(id)
      window.removeEventListener(NOTIFICATION_EVENT, onNotification)
    }
  }, [tab])

  // Tick so Duration advances between polls.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const rows = useMemo(() => {
    if (tab === "critical") return calls.filter((c) => categoryOf(c.status) === "critical")
    if (tab === "completed") return history
    return calls
  }, [tab, calls, history])

  // Stat cards from GET /calls/stats (same visibility as the list); zeros until it loads.
  const statCards = useMemo(() => {
    const cards: { label: string; value: number; icon: LucideIcon; tone?: "critical" }[] = [
      { label: "Total Calls Today", value: stats?.total_today ?? 0, icon: Phone },
      { label: "Active Calls", value: stats?.live ?? 0, icon: PhoneCall },
      {
        label: "Running Smoothly",
        value: (stats?.live ?? 0) - (stats?.critical ?? 0),
        icon: CheckCircle2,
      },
      { label: "Critical Alerts", value: stats?.critical ?? 0, icon: AlertCircle, tone: "critical" },
    ]
    return cards
  }, [stats])

  // Render the freshest polled row, falling back to the click-time snapshot once the call leaves the active list so its header survives while the modal is open.
  const modalCall = useMemo(() => {
    if (!selected) return null
    const fresh = calls.find((c) => c.id === selected.id) ?? selected
    return toLiveCall(fresh, now)
  }, [selected, calls, now])

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
    setSelected(call)
    setOverviewOpen(true)
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Live Monitoring</h1>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {statCards.map(({ label, value, icon: Icon, tone }) => (
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
              <TableHead>Call Health</TableHead>
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
                    {/* Ended calls show their fixed duration, not a still-running timer. */}
                    {elapsed(call.started_at, call.ended_at ? Date.parse(call.ended_at) : now)}
                  </TableCell>
                  <TableCell>
                    <CallHealthCell call={call} now={now} />
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
                      {/* Terminal calls open the same modal as a transcript replay. */}
                      {cat === "completed" ? "View" : call.is_owner ? "View Live" : "Intervene"}
                    </Button>
                  </TableCell>
                </TableRow>
              )
            })}
            {rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={8} className="py-10 text-center text-muted-foreground">
                  No calls in this view.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </Card>

      <LiveCallModal
        call={modalCall}
        open={overviewOpen}
        onOpenChange={setOverviewOpen}
        onExpand={() => {
          if (selected) openFormById(selected.form_id)
        }}
      />
    </div>
  )
}
