import { useEffect, useState, type ReactNode } from "react"
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"

import { MetricCard } from "@/components/analytics/MetricCard"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import {
  getHistoryReport,
  getReportFilters,
  type HistoryReport as HistoryReportData,
  type ReportFilterOptions,
} from "@/lib/api/analytics"
import { ApiError } from "@/lib/api/client"
import {
  deltaPct,
  formatDay,
  formatDuration,
  formatPct,
  mergeInterventionDays,
  presetRange,
  type DateRange,
  type PresetKey,
} from "@/lib/analytics/report"

const PRESETS: { key: PresetKey; label: string }[] = [
  { key: "7d", label: "Last 7 days" },
  { key: "30d", label: "Last 30 days" },
  { key: "90d", label: "Last 90 days" },
  { key: "week", label: "This week" },
  { key: "month", label: "This month" },
  { key: "custom", label: "Custom range" },
]

const BAR_COLOR = "#34B2B2" // brand teal, same literal Live Monitoring uses

// Stack order = array order; colors CVD-validated as adjacent pairs on white
// (dataviz six-checks validator) — change order and colors together.
const INTERVENTION_SERIES = [
  { key: "flag", label: "Flag", color: "#2a78d6" },
  { key: "coach", label: "Coach", color: "#eb6834" },
  { key: "whisper", label: "Whisper", color: "#0f9b9b" },
  { key: "takeover", label: "Takeover", color: "#c98500" },
] as const

// Both charts share one axis, so both read the same config.
const DAY_AXIS = {
  dataKey: "day",
  tickFormatter: formatDay,
  interval: "preserveStartEnd",
  minTickGap: 24,
  tickLine: false,
  axisLine: false,
  fontSize: 12,
} as const

const COUNT_AXIS = {
  allowDecimals: false,
  tickLine: false,
  axisLine: false,
  fontSize: 12,
} as const

function selectedRange(preset: PresetKey, customFrom: string, customTo: string): DateRange | null {
  if (preset !== "custom") return presetRange(preset, new Date())
  if (!customFrom || !customTo) return null
  // UTC-day widening, same convention as CallHistory's date filters.
  return { date_from: `${customFrom}T00:00:00Z`, date_to: `${customTo}T23:59:59Z` }
}

type ChartCardProps = {
  title: string
  /** Message to show instead of the chart when the series has no data. */
  empty?: string
  children: ReactNode
}

function ChartCard({ title, empty, children }: ChartCardProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="h-64">
        {empty ? (
          <p className="flex h-full items-center justify-center text-sm text-muted-foreground">
            {empty}
          </p>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            {children}
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  )
}

/** VR2-45: historical metrics with previous-period deltas and charts. */
export function HistoryReport() {
  const [preset, setPreset] = useState<PresetKey>("30d")
  const [customFrom, setCustomFrom] = useState("")
  const [customTo, setCustomTo] = useState("")
  const [providerId, setProviderId] = useState("")
  const [vaId, setVaId] = useState("")
  const [filters, setFilters] = useState<ReportFilterOptions | null>(null)
  const [report, setReport] = useState<HistoryReportData | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getReportFilters().then(setFilters).catch(() => setFilters({ providers: [], vas: [] }))
  }, [])

  useEffect(() => {
    const range = selectedRange(preset, customFrom, customTo)
    if (!range) return
    let cancelled = false
    getHistoryReport({
      ...range,
      provider_id: providerId || undefined,
      va_id: vaId || undefined,
    })
      .then((data) => {
        if (!cancelled) {
          setReport(data)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load the report.")
        }
      })
    return () => {
      cancelled = true
    }
  }, [preset, customFrom, customTo, providerId, vaId])

  const cur = report?.current
  const prev = report?.previous

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight">History Report</h2>
      <div className="flex flex-wrap items-end gap-3">
        <div className="grid gap-1.5">
          <Label htmlFor="report-range">Date range</Label>
          <Select
            id="report-range"
            value={preset}
            onChange={(e) => setPreset(e.target.value as PresetKey)}
          >
            {PRESETS.map((p) => (
              <option key={p.key} value={p.key}>
                {p.label}
              </option>
            ))}
          </Select>
        </div>
        {preset === "custom" && (
          <>
            <Input
              type="date"
              aria-label="From date"
              value={customFrom}
              onChange={(e) => setCustomFrom(e.target.value)}
              className="w-40"
            />
            <Input
              type="date"
              aria-label="To date"
              value={customTo}
              onChange={(e) => setCustomTo(e.target.value)}
              className="w-40"
            />
          </>
        )}
        <div className="grid gap-1.5">
          <Label htmlFor="report-provider">Provider</Label>
          <Select
            id="report-provider"
            value={providerId}
            onChange={(e) => setProviderId(e.target.value)}
          >
            <option value="">All providers</option>
            {filters?.providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="grid gap-1.5">
          <Label htmlFor="report-va">VA</Label>
          <Select id="report-va" value={vaId} onChange={(e) => setVaId(e.target.value)}>
            <option value="">All VAs</option>
            {filters?.vas.map((v) => (
              <option key={v.id} value={v.id}>
                {v.name}
              </option>
            ))}
          </Select>
        </div>
      </div>
      {cur && prev && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            label="Call Volume"
            value={String(cur.call_volume)}
            deltaPct={deltaPct(cur.call_volume, prev.call_volume)}
          />
          <MetricCard
            label="Data Capture %"
            value={formatPct(cur.avg_completion_pct)}
            deltaPct={deltaPct(cur.avg_completion_pct, prev.avg_completion_pct)}
          />
          <MetricCard
            label="Intervention Rate"
            value={formatPct(cur.intervention_rate, { fraction: true })}
            deltaPct={deltaPct(cur.intervention_rate, prev.intervention_rate)}
            invert
          />
          <MetricCard
            label="Avg Call Duration"
            value={formatDuration(cur.avg_duration_seconds)}
            deltaPct={deltaPct(cur.avg_duration_seconds, prev.avg_duration_seconds)}
          />
        </div>
      )}
      {report && (
        <div className="grid gap-4 lg:grid-cols-2">
          <ChartCard
            title="Calls per day"
            empty={report.current.call_volume === 0 ? "No calls in this period" : undefined}
          >
            <BarChart data={report.calls_per_day}>
              <CartesianGrid vertical={false} strokeOpacity={0.3} />
              <XAxis {...DAY_AXIS} />
              <YAxis {...COUNT_AXIS} />
              <Tooltip cursor={{ fillOpacity: 0.1 }} />
              <Bar dataKey="calls" fill={BAR_COLOR} radius={[3, 3, 0, 0]} />
            </BarChart>
          </ChartCard>
          <ChartCard
            title="Interventions per day"
            empty={
              report.interventions_per_day.length === 0
                ? "No interventions in this period"
                : undefined
            }
          >
            <BarChart
              data={mergeInterventionDays(report.calls_per_day, report.interventions_per_day)}
            >
              <CartesianGrid vertical={false} strokeOpacity={0.3} />
              <XAxis {...DAY_AXIS} />
              <YAxis {...COUNT_AXIS} />
              <Tooltip cursor={{ fillOpacity: 0.1 }} />
              <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 12 }} />
              {INTERVENTION_SERIES.map((series) => (
                <Bar
                  key={series.key}
                  dataKey={series.key}
                  name={series.label}
                  stackId="interventions"
                  fill={series.color}
                  stroke="#fff"
                  strokeWidth={1}
                  maxBarSize={40}
                />
              ))}
            </BarChart>
          </ChartCard>
        </div>
      )}
      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
