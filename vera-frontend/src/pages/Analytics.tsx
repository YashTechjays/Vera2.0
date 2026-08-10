import { HistoryReport } from "@/components/analytics/HistoryReport"
import { LiveAnalyticsPanel } from "@/components/analytics/LiveAnalyticsPanel"

export function Analytics() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Analytics</h1>
      <LiveAnalyticsPanel />
      <HistoryReport />
    </div>
  )
}
