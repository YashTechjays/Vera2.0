import { useEffect, useState } from "react"

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api/client"
import { listApiKeys, type ApiKey } from "@/lib/api-keys"
import { formatDate } from "@/lib/patient-forms/display"

/** Read-only list of the tenant's inbound API keys. Mount only behind an
 *  `apikeys:manage` permission check — the endpoint is gated server-side too. */
export function ApiKeysSection() {
  const [keys, setKeys] = useState<ApiKey[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listApiKeys()
      .then((rows) => {
        if (!cancelled) {
          setKeys(rows)
          setError(null)
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load API keys.")
          setKeys([])
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <section className="space-y-2">
      <h2 className="text-sm font-medium">API Keys</h2>
      <p className="text-sm text-muted-foreground">
        Inbound keys external systems use to send data to Vera. The key value is shown only
        once, when the key is created.
      </p>

      {error && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      <div className="rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Name</TableHead>
              <TableHead>Key ID</TableHead>
              <TableHead>Scope</TableHead>
              <TableHead>Expires</TableHead>
              <TableHead>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {keys === null && (
              <TableRow>
                <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {keys?.length === 0 && !error && (
              <TableRow>
                <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                  No API keys yet.
                </TableCell>
              </TableRow>
            )}
            {keys?.map((k) => (
              <TableRow key={k.id}>
                <TableCell className="font-medium">{k.name}</TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">{k.id}</TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">
                  {k.scope}
                </TableCell>
                <TableCell className="text-muted-foreground">
                  {k.expires_at ? formatDate(k.expires_at) : "Never"}
                </TableCell>
                <TableCell>
                  <span
                    className={cn(
                      "inline-block rounded-full px-2.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide",
                      k.revoked
                        ? "bg-red-100 text-red-700"
                        : "bg-emerald-100 text-emerald-700",
                    )}
                  >
                    {k.revoked ? "Revoked" : "Active"}
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </section>
  )
}
