import { useMemo, useState } from "react"
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
import { CallOverviewModal } from "@/components/monitoring/CallOverviewModal"
import { InterveneModal } from "@/components/monitoring/InterveneModal"
import {
  stats,
  liveCalls,
  completedCalls,
  type CallAction,
  type CallCategory,
  type LiveCall,
} from "@/lib/mock-data"

type TabKey = "active" | "critical" | "completed"

const TABS: { key: TabKey; label: string }[] = [
  { key: "active", label: "Active" },
  { key: "critical", label: "Critical" },
  { key: "completed", label: "Completed" },
]

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

const actionLabel: Record<CallAction, string> = {
  intervene: "Intervene",
  view: "View Live",
  "add-info": "Add Info",
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
  const [tab, setTab] = useState<TabKey>("active")
  const [visible, setVisible] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(
      [...liveCalls, ...completedCalls].map((c) => [c.id, c.visible])
    )
  )
  const [overviewCall, setOverviewCall] = useState<LiveCall | null>(null)
  const [overviewOpen, setOverviewOpen] = useState(false)
  const [interveneOpen, setInterveneOpen] = useState(false)

  const rows = useMemo(() => {
    if (tab === "critical")
      return liveCalls.filter((c) => c.category === "critical")
    if (tab === "completed") return completedCalls
    return liveCalls
  }, [tab])

  const openOverview = (call: LiveCall) => {
    setOverviewCall(call)
    setOverviewOpen(true)
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
                  className={cn(
                    "size-5",
                    tone === "critical" ? "text-red-500" : "text-[#34B2B2]"
                  )}
                />
              </div>
              <div>
                <div
                  className={cn(
                    "text-2xl font-bold leading-tight",
                    tone === "critical" && "text-red-600"
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

      <Card>
        <div className="px-4">
          {/* Tabs */}
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
                    : "text-muted-foreground hover:text-foreground"
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
            {rows.map((call) => (
              <TableRow key={call.id} className={cn(rowTint[call.category])}>
                <TableCell className="font-medium">
                  <span className="flex items-center gap-2">
                    <CallIndicator category={call.category} />
                    {call.patient}
                  </span>
                </TableCell>
                <TableCell className="text-muted-foreground">{call.type}</TableCell>
                <TableCell>{call.agent}</TableCell>
                <TableCell
                  className={cn(
                    "font-semibold tabular-nums",
                    durationColor[call.category]
                  )}
                >
                  {call.duration}
                </TableCell>
                <TableCell>
                  <span
                    className={cn(
                      "inline-block max-w-[190px] truncate rounded-md px-2 py-1 text-xs font-medium",
                      badgeStyle[call.category]
                    )}
                    title={call.status}
                  >
                    {call.status}
                  </span>
                </TableCell>
                <TableCell>
                  <Switch
                    checked={visible[call.id] ?? false}
                    onCheckedChange={(v) =>
                      setVisible((prev) => ({ ...prev, [call.id]: v }))
                    }
                  />
                </TableCell>
                <TableCell>
                  <Button
                    size="sm"
                    variant={call.action === "view" ? "outline" : "default"}
                    onClick={() => openOverview(call)}
                  >
                    {actionLabel[call.action]}
                  </Button>
                </TableCell>
              </TableRow>
            ))}
            {rows.length === 0 && (
              <TableRow>
                <TableCell
                  colSpan={7}
                  className="py-10 text-center text-muted-foreground"
                >
                  No calls in this view.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </Card>

      <CallOverviewModal
        call={overviewCall}
        open={overviewOpen}
        onOpenChange={setOverviewOpen}
        onExpand={() => openForm()}
        onIntervene={() => {
          setOverviewOpen(false)
          setInterveneOpen(true)
        }}
      />

      <InterveneModal
        call={overviewCall}
        open={interveneOpen}
        onOpenChange={setInterveneOpen}
      />
    </div>
  )
}
