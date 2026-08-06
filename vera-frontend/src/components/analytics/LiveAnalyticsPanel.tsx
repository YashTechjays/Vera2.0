import { useEffect, useState } from "react"

import { Card } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { getLivePanel, type LivePanel } from "@/lib/api/analytics"
import { ApiError } from "@/lib/api/client"

// Same rhythm as Live Monitoring, so the two screens tick together.
const POLL_MS = 8000

/** VR2-44: live in-queue / active counts per provider, refreshed automatically. */
export function LiveAnalyticsPanel() {
  const [panel, setPanel] = useState<LivePanel | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const data = await getLivePanel()
        if (!cancelled) {
          setPanel(data)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load live activity.")
        }
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

  const rows = panel?.rows ?? []
  const totalQueued = rows.reduce((sum, r) => sum + r.in_queue, 0)
  const totalActive = rows.reduce((sum, r) => sum + r.active, 0)

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight">Live Activity</h2>
      <Card>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Provider</TableHead>
              <TableHead>In Queue</TableHead>
              <TableHead>Active</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.provider_id ?? "none"}>
                <TableCell>{row.provider_name ?? "(No provider)"}</TableCell>
                <TableCell>{row.in_queue}</TableCell>
                <TableCell>{row.active}</TableCell>
              </TableRow>
            ))}
            {panel && rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={3} className="text-muted-foreground">
                  No calls in queue or in progress right now.
                </TableCell>
              </TableRow>
            )}
            {rows.length > 0 && (
              <TableRow data-testid="live-totals" className="font-medium">
                <TableCell>Total</TableCell>
                <TableCell>{totalQueued}</TableCell>
                <TableCell>{totalActive}</TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </Card>
      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
