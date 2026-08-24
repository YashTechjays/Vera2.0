import { Minus, TrendingDown, TrendingUp } from "lucide-react"

import { Card, CardContent } from "@/components/ui/card"
import { cn } from "@/lib/utils"

type Props = {
  label: string
  value: string
  deltaPct: number | null
  /** For metrics where DOWN is the good direction (e.g. intervention rate). */
  invert?: boolean
}

export function MetricCard({ label, value, deltaPct, invert = false }: Props) {
  const display = deltaPct === null ? null : Math.abs(deltaPct).toFixed(1)
  // Neutral when the DISPLAYED delta is 0.0% — deciding off the raw value would
  // still pair "0.0%" with a colored arrow when it merely rounds to zero (VR2-283).
  const flat = display === "0.0"
  const up = deltaPct !== null && deltaPct > 0
  const good = up !== invert
  return (
    <Card size="sm">
      <CardContent>
        <p className="text-sm text-muted-foreground">{label}</p>
        <p className="text-2xl font-bold leading-tight">{value}</p>
        {display !== null && (
          <p
            className={cn(
              "mt-1 text-sm",
              flat ? "text-muted-foreground" : good ? "text-emerald-600" : "text-red-600",
            )}
          >
            {flat ? (
              <Minus aria-label="flat" className="mr-1 inline size-4" />
            ) : up ? (
              <TrendingUp aria-label="up" className="mr-1 inline size-4" />
            ) : (
              <TrendingDown aria-label="down" className="mr-1 inline size-4" />
            )}
            {display}% vs previous period
          </p>
        )}
      </CardContent>
    </Card>
  )
}
