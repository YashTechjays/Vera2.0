import { Card, CardContent } from "@/components/ui/card"
import type { QueueStatus } from "@/lib/api/analytics"

/** Why a queued call hasn't started: the tenant-wide active-call limit (VR2-44). */
export function QueueLimitCard({ status }: { status: QueueStatus | null }) {
  if (!status) return null
  const atCapacity = status.active >= status.limit
  const figures = [
    { label: "Active Call Queue Limit", value: status.limit },
    { label: "Active", value: status.active },
    { label: "In Queue", value: status.in_queue },
  ]
  return (
    <Card size="sm">
      <CardContent className="flex flex-wrap items-center gap-x-10 gap-y-2">
        {figures.map(({ label, value }) => (
          <div key={label}>
            <p className="text-sm text-muted-foreground">{label}</p>
            <p className="text-2xl font-bold leading-tight">{value}</p>
          </div>
        ))}
        {atCapacity && status.in_queue > 0 && (
          <p className="text-sm text-amber-600">
            All call slots are in use — queued calls start when a slot frees up.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
