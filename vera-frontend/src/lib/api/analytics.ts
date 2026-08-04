import { apiRequest } from "@/lib/api/client"

/** GET /analytics/queue-status — tenant-wide mirror of the dispatcher's slot math. */
export type QueueStatus = { limit: number; active: number; in_queue: number }

export function getQueueStatus(): Promise<QueueStatus> {
  return apiRequest<QueueStatus>("/analytics/queue-status")
}

export type LiveProviderRow = {
  provider_id: string | null
  provider_name: string | null
  in_queue: number
  active: number
}

/** GET /analytics/live — live counts per provider, same rules as Live Monitoring. */
export type LivePanel = { rows: LiveProviderRow[] }

export function getLivePanel(): Promise<LivePanel> {
  return apiRequest<LivePanel>("/analytics/live")
}

export type ReportMetrics = {
  call_volume: number
  avg_duration_seconds: number | null
  avg_completion_pct: number | null
  intervened_calls: number
  intervention_rate: number | null
}

export type HistoryReport = {
  current: ReportMetrics
  previous: ReportMetrics
  calls_per_day: { day: string; calls: number }[]
  interventions_by_type: { type: string; count: number }[]
}

export type ReportParams = {
  date_from: string
  date_to: string
  provider_id?: string
  va_id?: string
}

/** GET /analytics/report — metrics for the range plus the previous equal-length range. */
export function getHistoryReport(params: ReportParams): Promise<HistoryReport> {
  const qs = new URLSearchParams({ date_from: params.date_from, date_to: params.date_to })
  if (params.provider_id) qs.set("provider_id", params.provider_id)
  if (params.va_id) qs.set("va_id", params.va_id)
  return apiRequest<HistoryReport>(`/analytics/report?${qs.toString()}`)
}

export type FilterOption = { id: string; name: string }
export type ReportFilterOptions = { providers: FilterOption[]; vas: FilterOption[] }

/** GET /analytics/filters — provider catalog + call-owning users for the dropdowns. */
export function getReportFilters(): Promise<ReportFilterOptions> {
  return apiRequest<ReportFilterOptions>("/analytics/filters")
}
